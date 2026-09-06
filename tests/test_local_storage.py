from pathlib import Path

import pytest
from atlas.storage import LocalStorageService


def test_local_storage_lists_only_approved_root(tmp_path: Path) -> None:
    (tmp_path / "Projects").mkdir()
    (tmp_path / "note.txt").write_text("atlas")
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    listing = service.list_directory()

    assert listing["display_root"] == "/home/jaco/Workspace"
    assert [item["name"] for item in listing["entries"]] == ["Projects", "note.txt"]
    assert listing["entries"][0]["kind"] == "directory"
    assert listing["entries"][1]["size_bytes"] == 5


def test_local_storage_rejects_escape(tmp_path: Path) -> None:
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    with pytest.raises(ValueError):
        service.list_directory("..")


def test_local_storage_acquires_image_as_model_resource(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    acquired = service.acquire_file("photo.jpg")

    resource = acquired["resource"]
    assert resource["name"] == "photo.jpg"
    assert resource["media_type"] == "image/jpeg"
    assert resource["source"] == "local_workspace"
    assert resource["data_base64"] == "anBlZy1ieXRlcw=="


def test_local_storage_acquire_rejects_escape(tmp_path: Path) -> None:
    service = LocalStorageService(tmp_path, "/home/jaco/Workspace")

    with pytest.raises((FileNotFoundError, ValueError)):
        service.acquire_file("../outside.jpg")
