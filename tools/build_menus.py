#!/usr/bin/env python3
"""One-stop menu build: scrape Disney menus (with dietary + allergen
restrictions), summarize what changed vs the currently-published data, and —
once you've reviewed the diff — publish.

This wraps the individual steps so the whole flow is one command:

    1. fetch_disney_menus.py  → menus.json          scrape + derive restrictions
    2. [you] review the diff  → Beyond Compare       currently-published vs new
    3. publish.py menus.json  → menus-<sha>.json + manifest.json   (no commit)
    4. git add/commit/push in themed-content         live via GH Pages in ~10 min
    5. next Xcode build runs Scripts/sync-bundled-config.sh, refreshing the
       app's bundled fallback from the manifest — automatic, nothing to do.

Two-phase by design so a human gates the publish:

    tools/build_menus.py                 # fetch → summary → open Beyond Compare
    tools/build_menus.py --no-compare    # fetch → summary only (no GUI)
    tools/build_menus.py --publish       # publish the reviewed menus.json (no refetch)

The "currently-published" file compared against is the hashed menus file that
`manifest.json` points at — NOT the unhashed working `menus.json`, which is
just this script's output slot.

Run it with the repo's venv python so `requests` is available, e.g.
    ../.venv/bin/python3 tools/build_menus.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
DEFAULT_OUT = REPO / "menus.json"


def published_menus_path() -> Path | None:
    """The hashed menus file the manifest currently points at (the real
    source of truth), or None if menus were never published."""
    manifest = json.loads((REPO / "manifest.json").read_text())
    entry = next((f for f in manifest.get("files", []) if f.get("role") == "menus"), None)
    if not entry:
        return None
    p = REPO / entry["path"]
    return p if p.is_file() else None


def _iter_items(doc: dict):
    for r in doc.get("restaurants", []):
        for mp in r.get("mealPeriods", []) or []:
            for g in mp.get("groups", []) or []:
                gtype = g.get("type") or ""
                for it in g.get("items", []) or []:
                    yield r.get("id"), gtype, it


def _profile(doc: dict) -> dict:
    restos = {r.get("id") for r in doc.get("restaurants", [])}
    items = tagged = with_allergens = af_items = 0
    for _rid, gtype, it in _iter_items(doc):
        items += 1
        if it.get("tags"):
            tagged += 1
        if it.get("allergenFriendlyFor"):
            with_allergens += 1
        if "allerg" in gtype.lower():
            af_items += 1
    return {
        "formatVersion": doc.get("formatVersion"),
        "restaurants": restos,
        "items": items,
        "tagged": tagged,
        "with_allergens": with_allergens,
        "af_items": af_items,
    }


def summarize(old_path: Path | None, new_path: Path) -> None:
    """Semantic rollup to complement Beyond Compare's line diff."""
    new = _profile(json.loads(new_path.read_text()))
    print("\n" + "=" * 60)
    print("MENU BUILD SUMMARY")
    print("=" * 60)
    if old_path is None:
        print("(no currently-published menus to compare against)")
        old = None
    else:
        old = _profile(json.loads(old_path.read_text()))
        added = new["restaurants"] - old["restaurants"]
        removed = old["restaurants"] - new["restaurants"]
        print(f"published : {old_path.name}  (formatVersion {old['formatVersion']})")
        print(f"new       : {new_path.name}  (formatVersion {new['formatVersion']})")
        print(f"restaurants : {len(old['restaurants'])} → {len(new['restaurants'])}"
              f"   (+{len(added)} / -{len(removed)})")
        if added:
            print("   added  : " + ", ".join(sorted(added)))
        if removed:
            print("   removed: " + ", ".join(sorted(removed)))

    def line(label, key):
        if old is None:
            print(f"{label:26}: {new[key]}")
        else:
            print(f"{label:26}: {old[key]} → {new[key]}")

    line("menu items", "items")
    line("items with dietary tags", "tagged")
    line("items with safe-for allergens", "with_allergens")
    line("allergy-friendly items", "af_items")
    print("=" * 60)


def open_beyond_compare(old_path: Path | None, new_path: Path) -> None:
    if old_path is None:
        print("nothing published yet — skipping Beyond Compare.")
        return
    launcher = shutil.which("bcompare") or shutil.which("bcomp")
    if not launcher:
        print("\nBeyond Compare CLI not found. Compare manually:")
        print(f"  {old_path}")
        print(f"  {new_path}")
        return
    print(f"\nopening Beyond Compare: {old_path.name}  vs  {new_path.name}")
    # Non-blocking: the GUI opens and this script returns.
    subprocess.Popen([launcher, str(old_path), str(new_path)])


def run_fetch(out: Path, api_date: str) -> None:
    print(f"fetching Disney menus for {api_date} → {out.name} …")
    subprocess.run(
        [sys.executable, str(TOOLS / "fetch_disney_menus.py"),
         "--out", str(out), "--date", api_date],
        check=True,
    )


def run_publish(out: Path) -> None:
    print(f"publishing {out.name} …")
    subprocess.run(
        [sys.executable, str(TOOLS / "publish.py"), str(out), "--role", "menus"],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"where to write the generated menus (default {DEFAULT_OUT.name})")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="menu date to request (default today)")
    ap.add_argument("--no-compare", action="store_true",
                    help="skip auto-launching Beyond Compare after the fetch")
    ap.add_argument("--compare-only", action="store_true",
                    help="don't fetch — just summarize + open Beyond Compare on the "
                         "existing --out file (review a candidate you already built)")
    ap.add_argument("--publish", action="store_true",
                    help="publish the already-generated --out file (no refetch), "
                         "then print git next-steps")
    args = ap.parse_args()

    published = published_menus_path()

    if args.compare_only:
        if not args.out.is_file():
            raise SystemExit(f"nothing to compare: {args.out} does not exist "
                             f"(run without a flag to generate it first)")
        summarize(published, args.out)
        open_beyond_compare(published, args.out)
        return

    if args.publish:
        if not args.out.is_file():
            raise SystemExit(f"nothing to publish: {args.out} does not exist "
                             f"(run without --publish first to generate it)")
        run_publish(args.out)
        print("\nNext: review `git diff` in themed-content, then commit & push.")
        print("The app's bundled fallback refreshes automatically on the next Xcode build.")
        return

    run_fetch(args.out, args.date)
    summarize(published, args.out)
    if not args.no_compare:
        open_beyond_compare(published, args.out)
    print("\nReviewed and happy? Publish with:")
    print("  .venv/bin/python3 tools/build_menus.py --publish")


if __name__ == "__main__":
    main()
