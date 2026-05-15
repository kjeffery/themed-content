#!/usr/bin/env python3
"""Verify manifest.json is internally consistent and in sync with its sources.

Run from the themed-content repo root (or anywhere — `--repo` defaults to the
parent of this script):

    tools/check_manifest.py

Exits non-zero if any of the following hold:
  - manifest.json is missing or malformed
  - a role's hashed file is missing from disk
  - a hashed file's actual SHA-256 or size disagrees with the manifest entry
  - a role's working `<role>.json` exists but its bytes diverge from the
    hashed file the manifest points at (the "imported but forgot to publish"
    case — run `tools/publish.py <role>.json` to resolve)

Reports every problem found before exiting; doesn't fail-fast.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="path to the themed-content repo (defaults to this script's parent)",
    )
    args = parser.parse_args()
    repo: Path = args.repo

    manifest_path = repo / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: manifest.json is not valid JSON: {e}", file=sys.stderr)
        return 1

    errors: list[str] = []
    files = manifest.get("files", [])
    if not isinstance(files, list):
        print("error: manifest.json 'files' must be a list", file=sys.stderr)
        return 1

    seen_roles: set[str] = set()

    for entry in files:
        role = entry.get("role")
        path_str = entry.get("path")
        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")

        if not (role and path_str and expected_sha and expected_bytes is not None):
            errors.append(f"manifest entry missing required field: {entry!r}")
            continue

        if role in seen_roles:
            errors.append(f"role '{role}' appears more than once in manifest")
        seen_roles.add(role)

        hashed = repo / path_str
        if not hashed.is_file():
            errors.append(f"role '{role}': manifest references {path_str}, but file is missing")
            continue

        actual_sha = sha256_file(hashed)
        if actual_sha != expected_sha:
            errors.append(
                f"role '{role}': {path_str} sha256 mismatch — manifest says "
                f"{expected_sha}, file is {actual_sha}"
            )

        actual_bytes = hashed.stat().st_size
        if actual_bytes != expected_bytes:
            errors.append(
                f"role '{role}': {path_str} size mismatch — manifest says "
                f"{expected_bytes} bytes, file is {actual_bytes}"
            )

        # Working-copy drift: if the unhashed source file exists, its bytes
        # must match the hashed copy. The .gitignore excludes auto-generated
        # roles (graph/closures/weights/menus), so this typically only fires
        # for hand-curated roles like ride_metadata and lands — exactly where
        # the import-without-publish gap was hiding.
        unhashed = repo / f"{role}.json"
        if unhashed.is_file():
            unhashed_sha = sha256_file(unhashed)
            if unhashed_sha != expected_sha:
                errors.append(
                    f"role '{role}': {role}.json diverges from manifest's "
                    f"{path_str} (run `tools/publish.py {role}.json` to publish)"
                )

    if errors:
        print(f"check_manifest: {len(errors)} problem(s) found:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print(f"check_manifest: OK ({len(files)} role(s) verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
