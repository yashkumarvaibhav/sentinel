"""The release-backed DVC bootstrap is bounded and checksum locked."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from lab.captures.bootstrap import BootstrapManifest, install_bootstrap


def test_bootstrap_installs_and_reuses_a_verified_dvc_remote(tmp_path: Path) -> None:
    archive, manifest = _archive(tmp_path)
    existing = tmp_path / "var" / "dvc-remote" / _root_object_path(manifest.dvc_directory_hash)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"root")
    existing.chmod(0o444)

    remote = install_bootstrap(repo_root=tmp_path, manifest=manifest, archive=archive)
    second = install_bootstrap(repo_root=tmp_path, manifest=manifest, archive=archive)

    assert second == remote
    assert (remote / _root_object_path(manifest.dvc_directory_hash)).read_bytes() == b"root"


def test_bootstrap_rejects_archive_hash_mismatch(tmp_path: Path) -> None:
    archive, manifest = _archive(tmp_path)

    with pytest.raises(ValueError, match="SHA-256"):
        install_bootstrap(
            repo_root=tmp_path,
            manifest=manifest.model_copy(update={"archive_sha256": "f" * 64}),
            archive=archive,
        )


def test_bootstrap_rejects_archive_path_traversal(tmp_path: Path) -> None:
    _, manifest = _archive(tmp_path)
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as bundle:
        info = tarfile.TarInfo("../escape")
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"root"))
    value = archive.read_bytes()
    unsafe = manifest.model_copy(
        update={
            "archive_name": archive.name,
            "archive_sha256": hashlib.sha256(value).hexdigest(),
            "archive_size_bytes": len(value),
        }
    )

    with pytest.raises(ValueError, match="unsafe bootstrap archive path"):
        install_bootstrap(repo_root=tmp_path, manifest=unsafe, archive=archive)
    assert not (tmp_path / "escape").exists()


def _archive(tmp_path: Path) -> tuple[Path, BootstrapManifest]:
    directory_hash = hashlib.md5(b"root", usedforsecurity=False).hexdigest() + ".dir"
    relative = Path("dvc-remote") / _root_object_path(directory_hash)
    archive = tmp_path / "bootstrap.tar.gz"
    with tarfile.open(archive, mode="w:gz") as bundle:
        info = tarfile.TarInfo(relative.as_posix())
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"root"))
    value = archive.read_bytes()
    manifest = BootstrapManifest(
        version=1,
        repository="owner/repository",
        release_tag="capture-v1",
        archive_name="bootstrap.tar.gz",
        archive_sha256=hashlib.sha256(value).hexdigest(),
        archive_size_bytes=len(value),
        remote_root="dvc-remote",
        payload_files=1,
        payload_size_bytes=4,
        dvc_directory_hash=directory_hash,
    )
    return archive, manifest


def _root_object_path(directory_hash: str) -> Path:
    digest = directory_hash.removesuffix(".dir")
    return Path("files") / "md5" / digest[:2] / f"{digest[2:]}.dir"
