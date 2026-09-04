#!/usr/bin/env python3
"""Validate shipped versions and build the deterministic HACS release archive.

Reads ``.release.json`` through ``scripts.release_config``. ``validate_versions``
is the independent reader the release gate relies on; the writer is
``set_version.py``. Stable ordering, timestamps, permissions, and compression
make the same source tree produce the same SHA-256 digest on every runner.

Usage:
    python -m scripts.build_release_artifacts --validate-only
    python -m scripts.build_release_artifacts --output dist/davis_vantage.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import release_config  # noqa: E402
from scripts.release_config import ReleaseConfig, load  # noqa: E402

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_SKIP_PARTS = {"__pycache__", "node_modules"}


def _config(target: Path | ReleaseConfig) -> ReleaseConfig:
    return target if isinstance(target, ReleaseConfig) else load(Path(target))


def validate_versions(target: Path | ReleaseConfig) -> str:
    """Return the single shipped version or raise on drift or bad format."""
    return release_config.validate_versions(_config(target))


def _release_files(source: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in source.rglob("*")
            if path.is_file()
            and not _SKIP_PARTS.intersection(path.relative_to(source).parts)
            and path.suffix not in {".pyc", ".pyo"}
        ),
        key=lambda path: path.relative_to(source).as_posix(),
    )


def _archive_label(source: Path) -> str:
    """Return the integration's display name, falling back to the directory."""
    manifest = source / "manifest.json"
    if manifest.exists():
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        if isinstance(name, str) and name.isascii():
            return name
    return source.name


def build_archive(target: Path | ReleaseConfig, output: Path) -> tuple[str, str]:
    """Write the archive and return ``(version, sha256)``."""
    config = _config(target)
    version = validate_versions(config)
    if not config.archive_source:
        raise ValueError(".release.json has no archive section")
    source = config.repository / config.archive_source
    files = _release_files(source)
    if not files or source / "manifest.json" not in files:
        raise ValueError("Release source is empty or missing manifest.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        archive.comment = f"{_archive_label(source)} {version}".encode("ascii")
        for path in files:
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIME)
            info.create_system = 3
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800  # UTF-8 file names
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return version, hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's repository)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="archive path; defaults to dist/<archive name>",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="print the shipped version and exit",
    )
    args = parser.parse_args()
    config = load(args.repository)
    if args.validate_only:
        print(validate_versions(config))
        return 0
    output = args.output or (
        config.repository / "dist" / (config.archive_name or "release.zip")
    )
    if not output.is_absolute():
        output = config.repository / output
    version, digest = build_archive(config, output)
    print(f"archive={output}")
    print(f"version={version}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
