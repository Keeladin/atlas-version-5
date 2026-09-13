import subprocess
from pathlib import Path

from atlas.integrations import repository_status as module


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "Atlas version 5"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "atlas@test.invalid")
    _git(path, "config", "user.name", "Atlas Test")
    (path / "README.md").write_text("atlas\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "Initial Atlas commit")
    _git(path, "remote", "add", "origin", "https://github.com/Keeladin/atlas-version-5.git")
    sha = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/origin/main", sha)
    return path, sha


def test_local_checkout_projects_owner_path_and_relation(tmp_path: Path) -> None:
    path, sha = _repo(tmp_path)
    result = module.local_checkouts(tmp_path, "/home/jaco/Projects", "Keeladin/atlas-version-5", "main", sha)
    assert len(result) == 1
    item = result[0]
    assert item["path"] == "/home/jaco/Projects/Atlas version 5"
    assert item["branch"] == "main" and item["short_sha"] == sha[:8]
    assert item["relation"] == "in_sync" and item["ahead"] == item["behind"] == 0
    assert item["dirty"] is False and item["remote_tracking_current"] is True
    (path / "README.md").write_text("changed\n")
    assert module.local_checkouts(tmp_path, "/home/jaco/Projects", "Keeladin/atlas-version-5", "main", sha)[0]["dirty"] is True



def test_linked_worktree_gitdir_is_remapped_from_owner_path(tmp_path: Path) -> None:
    main, sha = _repo(tmp_path)
    worktree = tmp_path / "atlas-v5-notifications"
    _git(main, "worktree", "add", "-b", "owner-notifications-v1", str(worktree))
    marker = worktree / ".git"
    actual_gitdir = Path(marker.read_text().split(":", 1)[1].strip())
    relative = actual_gitdir.relative_to(tmp_path)
    marker.write_text(f"gitdir: /home/jaco/Projects/{relative}\n")

    result = module.local_checkouts(tmp_path, "/home/jaco/Projects", "Keeladin/atlas-version-5", "main", sha)
    by_path = {item["path"]: item for item in result}
    assert "/home/jaco/Projects/Atlas version 5" in by_path
    linked = by_path["/home/jaco/Projects/atlas-v5-notifications"]
    assert linked["branch"] == "owner-notifications-v1" and linked["sha"] == sha

def test_repository_status_combines_remote_ci_and_local(monkeypatch, tmp_path: Path) -> None:
    _, sha = _repo(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret\n")

    def fake_github_json(_token_file, path):
        if "/check-runs" in path:
            return {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success", "html_url": "https://example/check"}]}
        if path.endswith("/status"):
            return {"state": "success", "statuses": []}
        return {"sha": sha, "html_url": "https://example/commit", "commit": {"message": "Ship repository status\n\nbody", "author": {"date": "2026-09-13T15:00:00Z"}}}

    monkeypatch.setattr(module, "github_json", fake_github_json)
    result = module.repository_status(
        token_file=token,
        projects_root=tmp_path,
        projects_display_root="/home/jaco/Projects",
        full_name="Keeladin/atlas-version-5",
        default_branch="main",
    )
    assert result["remote"]["sha"] == sha and result["remote"]["subject"] == "Ship repository status"
    assert result["ci"]["state"] == "success" and result["ci"]["checks"] == 1
    assert result["local"][0]["relation"] == "in_sync"


def test_ci_failure_wins_over_pending() -> None:
    result = module._ci_state(
        {"check_runs": [{"name": "lint", "status": "completed", "conclusion": "failure"}, {"name": "build", "status": "in_progress", "conclusion": None}]},
        {"state": "pending", "statuses": [{}]},
    )
    assert result["state"] == "failure"
