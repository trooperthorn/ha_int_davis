#!/usr/bin/env python3
"""Calculate and apply the synchronized CalVer release version.

This is the only writer for the version fields listed in ``.release.json``.
``build_release_artifacts.validate_versions`` is the independent reader the
release gate uses, so a defect here cannot validate itself.

Usage:
    python -m scripts.set_version --next-from-tags
    python -m scripts.set_version --version 2026.09.03.2
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_config import (  # noqa: E402
    CALVER_RE,
    ReleaseConfig,
    load,
    validate_versions,
)

_STRICT_CALVER_RE = re.compile(
    r"^(?P<year>[0-9]{4})\.(?P<month>[0-9]{2})\.(?P<day>[0-9]{2})\."
    r"(?P<sequence>[1-9][0-9]*)$"
)


def _config(target: Path | ReleaseConfig) -> ReleaseConfig:
    return target if isinstance(target, ReleaseConfig) else load(Path(target))


def parse_calver(value: str) -> tuple[date, int]:
    """Parse ``YYYY.MM.DD.N`` and reject invalid dates or zero sequences."""
    match = _STRICT_CALVER_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid CalVer release version: {value}")
    release_date = date(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
    )
    return release_date, int(match.group("sequence"))


def next_calver(existing: list[str], release_date: date, prefix: str = "v") -> str:
    """Return the next sequence for ``release_date`` from existing versions."""
    sequences: list[int] = []
    for value in existing:
        bare = value[len(prefix) :] if prefix and value.startswith(prefix) else value
        match = CALVER_RE.fullmatch(bare)
        if match is None:
            continue
        try:
            version_date = date(
                int(match["year"]), int(match["month"]), int(match["day"])
            )
        except ValueError:
            continue
        if version_date == release_date:
            sequences.append(int(match["sequence"]) if match["sequence"] else 0)
    return f"{release_date:%Y.%m.%d}.{max(sequences, default=0) + 1}"


def versions_from_git_tags(repository: Path, prefix: str = "v") -> list[str]:
    """Read immutable version candidates from the repository's local tags."""
    result = subprocess.run(
        ["git", "tag", "--list", f"{prefix}[0-9]*"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected exactly one {label}; found {count}")
    return updated


def set_version(target: Path | ReleaseConfig, version: str) -> None:
    """Set every shipped version field, preserving file modes."""
    parse_calver(version)
    config = _config(target)
    rendered: dict[Path, str] = {}
    for field in config.version_fields:
        path = config.repository / field.path
        text = path.read_text(encoding="utf-8")
        if field.kind == "json":
            data = json.loads(text)
            if not isinstance(data.get(field.key), str):
                raise ValueError(f"{field.path} has no string {field.key}")
            data[field.key] = version
            rendered[path] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        else:
            rendered[path] = _replace_one(
                text,
                field.pattern or "",
                (field.template or "").format(version=version),
                field.path,
            )
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    if validate_versions(config) != version:
        raise ValueError("Version synchronization failed after writing files")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's repository)",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--version", help="explicit YYYY.MM.DD.N version")
    selection.add_argument(
        "--next-from-tags",
        action="store_true",
        help="calculate today's next sequence from local Git tags",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone for --next-from-tags; defaults to .release.json",
    )
    args = parser.parse_args()
    config = load(args.repository)

    version = args.version
    if args.next_from_tags:
        zone = args.timezone or config.timezone
        try:
            release_date = datetime.now(ZoneInfo(zone)).date()
        except ZoneInfoNotFoundError as err:
            print(
                f"warning: zone {zone} unavailable ({err}); using the local "
                "system date. Install tzdata to fix.",
                file=sys.stderr,
            )
            release_date = datetime.now().astimezone().date()
        version = next_calver(
            versions_from_git_tags(config.repository, config.tag_prefix),
            release_date,
            config.tag_prefix,
        )
    assert version is not None
    set_version(config, version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
