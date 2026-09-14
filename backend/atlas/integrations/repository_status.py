"""Read-only local/remote repository status for the owner repository browser."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _git(path: Path, *args: str, check: bool = True, git_dir: Path | None = None) -> str:
    location = ["--git-dir", str(git_dir), "--work-tree", str(path)] if git_dir else ["-C", str(path)]
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={path}", *location, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()




def _mapped_git_dir(path: Path, projects_root: Path, display_root: str) -> Path | None:
    marker = path / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        return (path / target).resolve(strict=False)
    try:
        relative = target.relative_to(Path(display_root))
    except ValueError:
        return target
    return projects_root / relative

def _remote_full_name(url: str) -> str | None:
    value = url.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.split(":", 1)[1]
    if value.startswith("ssh://git@github.com/"):
        return value.split("github.com/", 1)[1]
    if "github.com/" in value:
        return value.split("github.com/", 1)[1].split("?", 1)[0]
    return None


def _local_checkout(path: Path, projects_root: Path, display_root: str, default_branch: str | None, remote_sha: str | None) -> dict[str, Any] | None:
    try:
        git_dir = _mapped_git_dir(path, projects_root, display_root)
        origin = _git(path, "config", "--get", "remote.origin.url", git_dir=git_dir)
        full_name = _remote_full_name(origin)
        sha = _git(path, "rev-parse", "HEAD", git_dir=git_dir)
        branch = _git(path, "branch", "--show-current", git_dir=git_dir) or "detached"
        subject = _git(path, "show", "-s", "--format=%s", "HEAD", git_dir=git_dir)
        dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=normal", check=False, git_dir=git_dir))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None

    tracking_sha = None
    ahead = behind = None
    relation = "unknown"
    if default_branch:
        tracking_ref = f"refs/remotes/origin/{default_branch}"
        tracking_sha = _git(path, "rev-parse", "--verify", tracking_ref, check=False, git_dir=git_dir) or None
        if tracking_sha:
            counts = _git(path, "rev-list", "--left-right", "--count", f"HEAD...{tracking_ref}", check=False, git_dir=git_dir).split()
            if len(counts) == 2 and all(part.isdigit() for part in counts):
                ahead, behind = map(int, counts)
                relation = "in_sync" if ahead == behind == 0 else "ahead" if ahead and not behind else "behind" if behind and not ahead else "diverged"
    if remote_sha and sha == remote_sha:
        relation, ahead, behind = "in_sync", 0, 0

    return {
        "path": str(path),
        "full_name": full_name,
        "branch": branch,
        "sha": sha,
        "short_sha": sha[:8],
        "subject": subject,
        "dirty": dirty,
        "tracking_sha": tracking_sha,
        "remote_tracking_current": bool(remote_sha and tracking_sha == remote_sha),
        "ahead": ahead,
        "behind": behind,
        "relation": relation,
    }


def local_checkouts(projects_root: Path, display_root: str, full_name: str, default_branch: str | None, remote_sha: str | None) -> list[dict[str, Any]]:
    if not projects_root.is_dir():
        return []
    wanted = full_name.casefold()
    result = []
    for path in sorted((item for item in projects_root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        if not (path / ".git").exists():
            continue
        checkout = _local_checkout(path, projects_root, display_root, default_branch, remote_sha)
        if checkout and str(checkout.get("full_name") or "").casefold() == wanted:
            try:
                relative = path.relative_to(projects_root)
                checkout["path"] = str(Path(display_root) / relative)
            except ValueError:
                pass
            result.append(checkout)
    return result


def github_json(token_file: Path, path: str) -> Any:
    token = token_file.read_text().strip()
    if not token:
        raise RuntimeError("GitHub credential is empty")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Atlas-V5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub API returned {exc.code}: {detail}") from exc


def _ci_state(checks: dict[str, Any], statuses: dict[str, Any]) -> dict[str, Any]:
    runs = [item for item in checks.get("check_runs", []) if isinstance(item, dict)]
    status_items = [item for item in statuses.get("statuses", []) if isinstance(item, dict)]
    pending = any(item.get("status") != "completed" for item in runs) or (bool(status_items) and statuses.get("state") == "pending")
    failed_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
    failed = any(item.get("conclusion") in failed_conclusions for item in runs) or (bool(status_items) and statuses.get("state") in {"failure", "error"})
    if failed:
        state = "failure"
    elif pending:
        state = "pending"
    elif runs or status_items:
        state = "success"
    else:
        state = "none"
    return {
        "state": state,
        "checks": len(runs),
        "statuses": len(status_items),
        "details": [
            {
                "name": str(item.get("name") or "check"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "url": item.get("html_url") or item.get("details_url"),
            }
            for item in runs[:12]
        ],
    }


def repository_status(*, token_file: Path, projects_root: Path, projects_display_root: str, full_name: str, default_branch: str | None) -> dict[str, Any]:
    branch = default_branch or "HEAD"
    quoted_repo = "/".join(urllib.parse.quote(part, safe="") for part in full_name.split("/"))
    commit = github_json(token_file, f"/repos/{quoted_repo}/commits/{urllib.parse.quote(branch, safe='')}")
    remote_sha = str(commit.get("sha") or "")
    if not remote_sha:
        raise RuntimeError("GitHub did not return a commit SHA")
    checks = github_json(token_file, f"/repos/{quoted_repo}/commits/{remote_sha}/check-runs?per_page=100")
    statuses = github_json(token_file, f"/repos/{quoted_repo}/commits/{remote_sha}/status")
    message = str((commit.get("commit") or {}).get("message") or "").splitlines()[0]
    author_date = (((commit.get("commit") or {}).get("author") or {}).get("date"))
    return {
        "remote": {
            "branch": default_branch,
            "sha": remote_sha,
            "short_sha": remote_sha[:8],
            "subject": message,
            "committed_at": author_date,
            "url": commit.get("html_url"),
        },
        "ci": _ci_state(checks if isinstance(checks, dict) else {}, statuses if isinstance(statuses, dict) else {}),
        "local": local_checkouts(projects_root, projects_display_root, full_name, default_branch, remote_sha),
    }
