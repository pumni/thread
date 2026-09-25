import os
from pathlib import Path

import pytest

from threads_platform.domain.browser_media import BrowserMediaKind
from threads_platform.infrastructure.worker_agent.local_media import (
    LocalMediaFileError,
    LocalMediaFileResolver,
)
from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot


def test_local_media_resolves_approved_file_without_exposing_path_in_repr(tmp_path: Path) -> None:
    data_root = LocalDataRoot(tmp_path / "worker-data")
    data_root.prepare()
    media_file = data_root.child("media", "sample.JPG")
    media_file.write_bytes(b"synthetic image")

    resolved = LocalMediaFileResolver(data_root).resolve("sample.JPG")

    assert resolved.media_ref == "sample.JPG"
    assert resolved.kind is BrowserMediaKind.IMAGE
    assert resolved.byte_size == len(b"synthetic image")
    assert resolved.path == media_file.resolve()
    assert str(media_file) not in repr(resolved)


@pytest.mark.parametrize(
    "media_ref",
    ["../secret.jpg", "..\\secret.jpg", "C:\\secret.jpg", "\\\\server\\share\\x.jpg", "CON.jpg"],
)
def test_local_media_rejects_traversal_drive_unc_and_device_paths(
    tmp_path: Path, media_ref: str
) -> None:
    data_root = LocalDataRoot(tmp_path / "worker-data")
    data_root.prepare()

    with pytest.raises(LocalMediaFileError) as error:
        LocalMediaFileResolver(data_root).resolve(media_ref)

    assert error.value.code == "MEDIA_FILE_REJECTED"
    assert str(tmp_path) not in str(error.value)


def test_local_media_rejects_non_media_empty_and_oversized_files(tmp_path: Path) -> None:
    data_root = LocalDataRoot(tmp_path / "worker-data")
    data_root.prepare()
    media_root = data_root.child("media")
    (media_root / "document.txt").write_text("not media", encoding="utf-8")
    (media_root / "empty.png").write_bytes(b"")
    (media_root / "large.png").write_bytes(b"1234")
    resolver = LocalMediaFileResolver(data_root, max_file_bytes=3)

    for media_ref in ("document.txt", "empty.png"):
        with pytest.raises(LocalMediaFileError) as error:
            resolver.resolve(media_ref)
        assert error.value.code == "MEDIA_FILE_REJECTED"
    with pytest.raises(LocalMediaFileError) as oversized:
        resolver.resolve("large.png")
    assert oversized.value.code == "MEDIA_FILE_TOO_LARGE"


def test_local_media_rejects_symlinks_escaping_the_managed_root(tmp_path: Path) -> None:
    data_root = LocalDataRoot(tmp_path / "worker-data")
    data_root.prepare()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"synthetic image")
    link = data_root.child("media", "linked.jpg")
    try:
        link.symlink_to(outside)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows does not permit symlink creation in this environment")
        raise

    with pytest.raises(LocalMediaFileError) as error:
        LocalMediaFileResolver(data_root).resolve("linked.jpg")

    assert error.value.code == "MEDIA_FILE_REJECTED"
    assert str(outside) not in str(error.value)
