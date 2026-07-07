#!/usr/bin/env python3
"""Render a menus.json into one human-readable Markdown report for review.

Two independent sections so each is easy to verify on its own:

  • MENUS — every restaurant, its meal periods, groups and items (price, any
    dietary tags). The "browse it like a menu" view.
  • ALLERGEN VERIFICATION — only the allergy-friendly items, each showing the
    PARSED safe-for allergen list right next to Disney's SOURCE description,
    so you can confirm the "(For X Allergies)" extraction item by item. Ends
    with a per-allergen tally as a distribution sanity check.

Reads a local file only — no network. Defaults to the working `menus.json`
candidate, falling back to the currently-published hashed file.

    tools/dump_menus.py                      # dump the candidate menus.json
    tools/dump_menus.py --menus some.json --out report.md
    tools/dump_menus.py --published          # dump what's live in the manifest
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dietary_signal import CANONICAL_ALLERGENS

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

PARK_LABEL = {"disneyland": "Disneyland", "california-adventure": "California Adventure"}
PRICE_TIER_GLYPH = {"low": "$", "medium": "$$", "high": "$$$", "luxury": "$$$$"}


def published_menus_path() -> Path | None:
    manifest = json.loads((REPO / "manifest.json").read_text())
    entry = next((f for f in manifest.get("files", []) if f.get("role") == "menus"), None)
    if not entry:
        return None
    p = REPO / entry["path"]
    return p if p.is_file() else None


def money(cents) -> str:
    return f"${cents / 100:.2f}" if isinstance(cents, int) else "—"


def restaurant_heading(r: dict) -> str:
    bits = []
    park = PARK_LABEL.get(r.get("park"))
    if park:
        bits.append(park)
    if r.get("land"):
        bits.append(r["land"])
    meta = []
    if r.get("priceTier"):
        meta.append(PRICE_TIER_GLYPH.get(r["priceTier"], r["priceTier"]))
    if r.get("isQuickService") is True:
        meta.append("Quick Service")
    elif r.get("isQuickService") is False:
        meta.append("Table Service")
    if r.get("cuisineTypes"):
        meta.append(", ".join(r["cuisineTypes"]))
    line = f"## {r.get('name', '?')}"
    sub = "  \n_" + " · ".join(bits + meta) + "_" if (bits or meta) else ""
    return line + sub


def render_menus(restaurants: list[dict], out: list[str]) -> None:
    out.append("# Menus\n")
    for r in restaurants:
        out.append(restaurant_heading(r))
        out.append("")
        for mp in r.get("mealPeriods", []) or []:
            out.append(f"### {mp.get('name', '')}")
            for g in mp.get("groups", []) or []:
                gtype = g.get("type")
                label = f"**{g.get('name', '')}**"
                if gtype:
                    label += f"  `{gtype}`"
                out.append(label)
                for it in g.get("items", []) or []:
                    tags = it.get("tags")
                    tag_str = f"  _[{', '.join(tags)}]_" if tags else ""
                    out.append(f"- {it.get('name', '?')} — {money(it.get('priceCents'))}{tag_str}")
                    if it.get("description"):
                        out.append(f"  {it['description']}")
                out.append("")
        out.append("---\n")


def render_allergens(restaurants: list[dict], out: list[str]) -> None:
    out.append("# Allergen verification\n")
    out.append("Allergy-friendly items only. **Safe for** is what the parser "
               "extracted; the quoted line is Disney's source description — they "
               "should agree. Absence of an allergen means *not claimed*, never "
               "*contains*. Always confirm with a cast member.\n")

    tally = {a: 0 for a in CANONICAL_ALLERGENS}
    any_af = False
    for r in restaurants:
        af_items = []
        for mp in r.get("mealPeriods", []) or []:
            for g in mp.get("groups", []) or []:
                if "allerg" not in (g.get("type") or g.get("name") or "").lower():
                    continue
                for it in g.get("items", []) or []:
                    af_items.append(it)
        if not af_items:
            continue
        any_af = True
        out.append(f"## {r.get('name', '?')}")
        for it in af_items:
            safe = it.get("allergenFriendlyFor")
            if safe:
                for a in safe:
                    if a in tally:
                        tally[a] += 1
                safe_str = ", ".join(safe)
            else:
                safe_str = "_(none parsed)_"
            out.append(f"- **{it.get('name', '?')}** — safe for: {safe_str}")
            if it.get("description"):
                out.append(f"  > {it['description']}")
        out.append("")

    if not any_af:
        out.append("_No allergy-friendly items found in this file._\n")
        return

    out.append("## Allergen tally\n")
    out.append("Items marked safe-for each allergen (distribution sanity check):\n")
    for a in CANONICAL_ALLERGENS:
        out.append(f"- {a}: {tally[a]}")
    out.append("")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--menus", type=Path, default=None,
                    help="menus.json to render (default: working menus.json, else published)")
    ap.add_argument("--published", action="store_true",
                    help="render the currently-published (manifest) menus instead")
    ap.add_argument("--out", type=Path, default=REPO / "menus_report.md")
    args = ap.parse_args()

    if args.menus:
        src = args.menus
    elif args.published:
        src = published_menus_path()
        if not src:
            raise SystemExit("no published menus in the manifest")
    else:
        candidate = REPO / "menus.json"
        src = candidate if candidate.is_file() else published_menus_path()
        if not src:
            raise SystemExit("no menus.json candidate and nothing published; run build_menus.py first")

    doc = json.loads(src.read_text())
    restaurants = sorted(doc.get("restaurants", []), key=lambda r: r.get("name", ""))

    n_af = sum(
        1 for r in restaurants
        for mp in r.get("mealPeriods", []) or []
        for g in mp.get("groups", []) or []
        if "allerg" in (g.get("type") or g.get("name") or "").lower()
    )

    out: list[str] = []
    out.append(f"# Disneyland Resort — Menus & Allergen Report")
    out.append(f"\n_Source: `{src.name}` (formatVersion {doc.get('formatVersion')}) · "
               f"{len(restaurants)} restaurants · {n_af} allergy-friendly groups._\n")
    out.append("- [Menus](#menus)")
    out.append("- [Allergen verification](#allergen-verification)\n")
    out.append("---\n")

    render_menus(restaurants, out)
    render_allergens(restaurants, out)

    args.out.write_text("\n".join(out) + "\n")
    print(f"wrote {args.out} ({len(restaurants)} restaurants, "
          f"{args.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
