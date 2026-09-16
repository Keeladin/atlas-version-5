from pathlib import Path

import pytest

from atlas.integrations import coding_mcp_server as coding


def test_coding_repo_must_be_inside_owner_approved_root(tmp_path, monkeypatch):
    allowed = tmp_path / "Projects"
    allowed.mkdir()
    repo = allowed / "Atlas V5"
    repo.mkdir()
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    monkeypatch.setenv("ATLAS_CODING_ROOTS", str(allowed))

    assert coding._validate_repo(repo) == repo.resolve()
    with pytest.raises(ValueError, match="outside approved coding roots"):
        coding._validate_repo(outside)


def test_coding_repo_symlink_cannot_escape_root(tmp_path, monkeypatch):
    allowed = tmp_path / "Workspace"
    allowed.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("ATLAS_CODING_ROOTS", str(allowed))

    with pytest.raises(ValueError, match="outside approved coding roots"):
        coding._validate_repo(link)


def test_coding_roots_use_platform_path_separator(tmp_path, monkeypatch):
    first = tmp_path / "Projects"
    second = tmp_path / "Workspace"
    first.mkdir()
    second.mkdir()
    project = second / "Morning"
    project.mkdir()
    monkeypatch.setenv("ATLAS_CODING_ROOTS", f"{first}{coding.os.pathsep}{second}")

    roots = coding._allowed_roots()
    assert roots == (first.resolve(), second.resolve())
    assert coding._validate_repo(project) == project.resolve()
