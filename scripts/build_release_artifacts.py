"""Build a deterministic HACS release archive for the Davis Vantage integration.

Used by .github/workflows/release.yml, which auto-computes the next
calendar version (see that workflow for the v<YYYY.MM.DD>.<build> scheme),
writes it into manifest.json with --set-version, and builds the archive HACS
installs from GitHub Releases. Runnable locally for a dry run:

    python3 scripts/build_release_artifacts.py --output dist/davis_vantage.zip
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

INTEGRATION_DIR = Path("custom_components/davis_vantage")
MANIFEST_PATH = INTEGRATION_DIR / "manifest.json"


def read_version(repo_root: Path) -> str:
    """Return the version currently recorded in manifest.json."""
    manifest = json.loads((repo_root / MANIFEST_PATH).read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not version:
        raise SystemExit(f"{MANIFEST_PATH} has no 'version' field")
    return version


def write_version(repo_root: Path, version: str) -> None:
    """Set manifest.json's version field, preserving every other key as-is."""
    path = repo_root / MANIFEST_PATH
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version"] = version
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def build_archive(repo_root: Path, output: Path) -> None:
    """Zip custom_components/davis_vantage/ deterministically for HACS.

    HACS installs the archive with its top-level entries treated as the
    integration's own files (no custom_components/davis_vantage/ prefix), and
    a fixed mtime + sorted file order keep the archive's bytes reproducible
    build-to-build for the same source tree, which is what the SBOM/
    attestation step signs against.
    """
    source = repo_root / INTEGRATION_DIR
    output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            arcname = file_path.relative_to(source)
            info = zipfile.ZipInfo(str(arcname))
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, file_path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--set-version",
        metavar="VERSION",
        help="Write this version into manifest.json before building.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    if args.set_version:
        write_version(repo_root, args.set_version)

    version = read_version(repo_root)
    print(f"Building davis_vantage.zip for version {version}")
    build_archive(repo_root, args.output)


if __name__ == "__main__":
    main()
