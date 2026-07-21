"""Install a checksum-locked DVC remote from the repository's private release."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type DvcDirectoryHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}\.dir$")]


class BootstrapManifest(BaseModel):
    """Immutable identity and safety bounds for one release asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    release_tag: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    archive_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.tar\.gz$")
    archive_sha256: Sha256
    archive_size_bytes: int = Field(gt=0)
    remote_root: Literal["dvc-remote"]
    payload_files: int = Field(gt=0)
    payload_size_bytes: int = Field(gt=0)
    dvc_directory_hash: DvcDirectoryHash


def load_manifest(path: Path) -> BootstrapManifest:
    return BootstrapManifest.model_validate_json(path.read_bytes())


def install_bootstrap(
    *,
    repo_root: Path,
    manifest: BootstrapManifest,
    archive: Path | None = None,
) -> Path:
    """Verify and merge immutable DVC objects into the configured local remote."""
    var_root = repo_root.resolve() / "var"
    remote = var_root / manifest.remote_root
    stamp = remote / f".sentinel-bootstrap-{manifest.archive_sha256}"
    if stamp.is_file() and _valid_root_object(remote, manifest.dvc_directory_hash):
        print(f"DVC bootstrap already verified: {manifest.release_tag}")
        return remote

    var_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dvc-bootstrap-", dir=var_root) as temporary:
        work = Path(temporary)
        selected_archive = (
            archive.resolve() if archive is not None else work / manifest.archive_name
        )
        if archive is None:
            _download(manifest, selected_archive)
        _verify_archive(selected_archive, manifest)
        extracted = work / "extracted"
        extracted.mkdir()
        with tarfile.open(selected_archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            _validate_members(members, manifest)
            bundle.extractall(extracted, members=members, filter="data")
        source = extracted / manifest.remote_root
        if not source.is_dir():
            raise ValueError(f"bootstrap archive omitted {manifest.remote_root}/")
        _merge_remote(source, remote)

    if not _valid_root_object(remote, manifest.dvc_directory_hash):
        raise ValueError("bootstrap DVC directory object failed its MD5 identity")
    stamp.write_text(manifest.archive_sha256 + "\n", encoding="utf-8")
    print(
        f"DVC bootstrap installed: {manifest.release_tag} "
        f"({manifest.payload_files} objects, {manifest.payload_size_bytes} bytes)"
    )
    return remote


def _download(manifest: BootstrapManifest, output: Path) -> None:
    subprocess.run(
        [
            "gh",
            "release",
            "download",
            manifest.release_tag,
            "--repo",
            manifest.repository,
            "--pattern",
            manifest.archive_name,
            "--output",
            str(output),
        ],
        check=True,
    )


def _verify_archive(path: Path, manifest: BootstrapManifest) -> None:
    if path.stat().st_size != manifest.archive_size_bytes:
        raise ValueError("bootstrap archive size does not match the committed manifest")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != manifest.archive_sha256:
        raise ValueError("bootstrap archive SHA-256 does not match the committed manifest")


def _validate_members(members: list[tarfile.TarInfo], manifest: BootstrapManifest) -> None:
    files = 0
    size_bytes = 0
    for member in members:
        candidate = PurePosixPath(member.name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"unsafe bootstrap archive path: {member.name!r}")
        if not candidate.parts or candidate.parts[0] != manifest.remote_root:
            raise ValueError(f"bootstrap archive path escapes remote root: {member.name!r}")
        if member.isfile():
            files += 1
            size_bytes += member.size
        elif not member.isdir():
            raise ValueError(f"bootstrap archive contains a non-file entry: {member.name!r}")
    if files != manifest.payload_files or size_bytes != manifest.payload_size_bytes:
        raise ValueError(
            "bootstrap payload bounds do not match the committed manifest: "
            f"files={files}, bytes={size_bytes}"
        )


def _valid_root_object(remote: Path, directory_hash: str) -> bool:
    digest = directory_hash.removesuffix(".dir")
    path = remote / "files" / "md5" / digest[:2] / f"{digest[2:]}.dir"
    return (
        path.is_file()
        and hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest() == digest
    )


def _merge_remote(source: Path, remote: Path) -> None:
    """Add immutable objects without overwriting DVC's read-only files."""
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        destination = remote / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (
                destination.stat().st_size != item.stat().st_size
                or hashlib.sha256(destination.read_bytes()).digest()
                != hashlib.sha256(item.read_bytes()).digest()
            ):
                raise ValueError(f"existing DVC remote object differs: {relative}")
            continue
        shutil.copy2(item, destination)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.captures.bootstrap")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    install_bootstrap(
        repo_root=args.repo_root,
        manifest=load_manifest(args.manifest),
        archive=args.archive,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
