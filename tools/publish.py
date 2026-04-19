#!/usr/bin/env python3
"""Publish a content file (graph / closures / weights) to the themed-content repo.

Usage:
    tools/publish.py <file> [--role <role>]

Hashes the file with SHA-256, copies it to the repo root as
`<role>-<sha16>.json`, and updates `manifest.json` to point at the new file.
Role is inferred from the source filename when possible ("closures.json" ->
closures), or supply `--role` explicitly.

The script does not commit. It prints a suggested git command at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

KNOWN_ROLES = ("graph", "closures", "weights")


def infer_role(path: Path) -> str:
    stem = path.stem
    # Accept both "closures.json" and "closures-<hash>.json".
    candidate = stem.split("-", 1)[0]
    if candidate in KNOWN_ROLES:
        return candidate
    raise SystemExit(
        f"cannot infer role from filename '{path.name}'; pass --role (one of {', '.join(KNOWN_ROLES)})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="the source JSON file to publish")
    parser.add_argument(
        "--role",
        choices=KNOWN_ROLES,
        help="override role inference (graph / closures / weights)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="path to the themed-content repo (defaults to this script's parent)",
    )
    args = parser.parse_args()

    src: Path = args.file
    if not src.is_file():
        raise SystemExit(f"not a file: {src}")

    role = args.role or infer_role(src)
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    filename = f"{role}-{digest[:16]}.json"
    repo: Path = args.repo
    manifest_path = repo / "manifest.json"

    # Fast-path: if manifest already points at this exact hash, nothing to do.
    manifest = json.loads(manifest_path.read_text())
    existing = next((f for f in manifest["files"] if f.get("role") == role), None)
    if existing and existing.get("sha256") == digest:
        print(f"no change: {role} is already at {existing['path']}")
        return

    # Copy the source into the repo under its content-addressed name.
    dest = repo / filename
    if dest.exists():
        if dest.read_bytes() != data:
            raise SystemExit(
                f"hash collision: {dest.name} exists with different content; refusing to overwrite"
            )
    else:
        shutil.copy2(src, dest)
        print(f"wrote {dest.relative_to(repo)} ({len(data)} bytes)")

    # Update manifest: drop any prior entry for this role, add the new one,
    # re-sort for stable diffs.
    manifest["files"] = [f for f in manifest["files"] if f.get("role") != role]
    manifest["files"].append(
        {
            "role": role,
            "path": filename,
            "sha256": digest,
            "bytes": len(data),
        }
    )
    manifest["files"].sort(key=lambda f: f["role"])
    manifest["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"updated manifest.json")

    if existing:
        old_path = existing["path"]
        print(f"note: old {role} file {old_path} is now unreferenced and may be deleted")

    print()
    print("Next steps:")
    print(f"  cd {repo}")
    print(f"  git add {filename} manifest.json")
    print(f'  git commit -m "Update {role}"')
    print(f"  git push")


if __name__ == "__main__":
    main()
