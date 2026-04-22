#!/usr/bin/env python3
"""Import OSM restrooms and shops into a graph JSON file.

Usage:
    tools/import_osm_pois.py <graph.json> [--dry-run] [--kinds restroom,shop]

Queries the Overpass API for `amenity=toilets` and `shop=*` elements clipped
to the Disneyland and Disney California Adventure park polygons, then merges
them into the graph as nodes with kind `restroom` / `shop`.

Idempotent — re-running updates names/coords for already-imported nodes
without duplicating. Imported nodes use ids of the form `osm-{kind}-{id}` so
they're easy to spot, and they carry no `themeParksEntityID` (OSM has no link
back to themeparks.wiki). Nodes are added without edges; connect them to the
path graph manually via the in-app debug overlay if you want them routable.

After importing, run `tools/enrich_elevations.py` to fill in elevations.

Filters applied:
- `access=private` is excluded (cast-only restrooms).
- `shop=vacant` is excluded (closed storefronts).
- Park assignment uses Overpass `map_to_area`, so anything outside the two
  park polygons (Downtown Disney, hotels, parking) is naturally skipped.

No auth required. Overpass is rate-limited but generous; this script makes
two requests total (one per park).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Theme-park boundary relations on OSM. Found via:
#   way[tourism=theme_park](around the resort).
PARKS = {
    # graph `park` raw value -> (OSM relation id, human label for logging)
    "disneyland": (5586855, "Disneyland"),
    "california-adventure": (15626312, "Disney California Adventure"),
}

# Tag values we never want to import. Cast-only restrooms aren't useful to
# guests; closed storefronts are noise.
EXCLUDED_TAG_VALUES = {
    ("access", "private"),
    ("shop", "vacant"),
}


def overpass_query(rel_id: int, kinds: list[str]) -> list[dict]:
    """Run a polygon-clipped Overpass query for the given OSM relation.

    Returns the raw element list. Each element has `type`, `id`, `tags` and
    either `lat`/`lon` (nodes) or `center` (ways/relations).
    """
    selectors = []
    if "restroom" in kinds:
        selectors.extend([
            'node["amenity"="toilets"](area.parkArea);',
            'way["amenity"="toilets"](area.parkArea);',
        ])
    if "shop" in kinds:
        selectors.extend([
            'node["shop"](area.parkArea);',
            'way["shop"](area.parkArea);',
        ])
    if not selectors:
        return []

    query = f"""
[out:json][timeout:60];
rel({rel_id})->.park;
.park map_to_area->.parkArea;
(
{chr(10).join("  " + s for s in selectors)}
);
out center tags;
"""
    body = urllib.parse.urlencode({"data": query}).encode()
    # Overpass rejects requests without a User-Agent (HTTP 406). The string
    # follows their convention of identifying the tool plus a contact pointer.
    req = urllib.request.Request(
        OVERPASS_URL,
        data=body,
        headers={"User-Agent": "themed-import-osm-pois/1.0 (github.com/kjeffery/themed-content)"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        payload = json.loads(response.read())
    return payload.get("elements", [])


def coord_for(element: dict) -> tuple[float, float] | None:
    if element["type"] == "node":
        return element["lat"], element["lon"]
    center = element.get("center")
    if center:
        return center["lat"], center["lon"]
    return None


def kind_for(element: dict) -> str | None:
    tags = element.get("tags", {})
    if tags.get("amenity") == "toilets":
        return "restroom"
    if "shop" in tags:
        return "shop"
    return None


def display_name(element: dict, kind: str) -> str:
    tags = element.get("tags", {})
    # Prefer `name`, then `ref` (Disney often uses ref="Toontown Restroom"
    # without a top-level name), then a generic fallback.
    name = tags.get("name") or tags.get("ref")
    if name:
        return name
    if kind == "restroom":
        return "Restroom"
    subtype = tags.get("shop", "shop").replace("_", " ").title()
    return f"{subtype} (shop)"


def is_excluded(element: dict) -> bool:
    tags = element.get("tags", {})
    return any(tags.get(k) == v for k, v in EXCLUDED_TAG_VALUES)


def stable_id(element: dict, kind: str) -> str:
    # OSM ids are stable across edits but not across element types — a node
    # and a way can share an id. Include the OSM type to disambiguate.
    return f"osm-{kind}-{element['type']}{element['id']}"


def make_node(element: dict, kind: str, park_raw: str) -> dict:
    lat, lon = coord_for(element)
    return {
        "id": stable_id(element, kind),
        "name": display_name(element, kind),
        "kind": kind,
        "coord": {"latitude": lat, "longitude": lon},
        "park": park_raw,
    }


def merge_nodes(graph: dict, candidates: list[dict]) -> tuple[int, int]:
    """Merge OSM-sourced nodes into the graph in-place.

    Returns (added, updated). Existing manual nodes (id not starting with
    `osm-`) are never touched.
    """
    by_id = {n["id"]: n for n in graph["nodes"]}
    added = 0
    updated = 0
    for candidate in candidates:
        existing = by_id.get(candidate["id"])
        if existing is None:
            graph["nodes"].append(candidate)
            added += 1
            continue
        # Preserve any fields the importer doesn't manage — notably
        # `elevationMeters` (filled in by enrich_elevations.py) and
        # `userEdited` (set by the in-app graph editor).
        merged = dict(existing)
        merged["name"] = candidate["name"]
        merged["coord"] = candidate["coord"]
        merged["park"] = candidate["park"]
        merged["kind"] = candidate["kind"]
        if merged != existing:
            existing.clear()
            existing.update(merged)
            updated += 1
    return added, updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("graph", type=Path, help="path to graph.json")
    parser.add_argument(
        "--kinds",
        default="restroom,shop",
        help="comma-separated list of kinds to import (restroom, shop)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without writing the graph file",
    )
    args = parser.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    invalid = [k for k in kinds if k not in {"restroom", "shop"}]
    if invalid:
        raise SystemExit(f"unknown kinds: {invalid} (allowed: restroom, shop)")

    graph = json.loads(args.graph.read_text())

    candidates: list[dict] = []
    for park_raw, (rel_id, label) in PARKS.items():
        print(f"querying Overpass for {label} (rel {rel_id})…", file=sys.stderr)
        elements = overpass_query(rel_id, kinds)
        kept = 0
        skipped_excluded = 0
        skipped_no_coord = 0
        for el in elements:
            kind = kind_for(el)
            if kind is None:
                continue
            if is_excluded(el):
                skipped_excluded += 1
                continue
            if coord_for(el) is None:
                skipped_no_coord += 1
                continue
            candidates.append(make_node(el, kind, park_raw))
            kept += 1
        print(
            f"  {label}: {kept} kept, "
            f"{skipped_excluded} excluded by tag, "
            f"{skipped_no_coord} missing coords",
            file=sys.stderr,
        )
        # Be a polite Overpass citizen — small gap between the two queries.
        time.sleep(1.0)

    added, updated = merge_nodes(graph, candidates)
    by_kind: dict[str, int] = {}
    for c in candidates:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    print(
        f"\nMerge result: {added} added, {updated} updated "
        f"(candidates by kind: {by_kind})"
    )

    if args.dry_run:
        print("(dry run — graph file not written)")
        return

    args.graph.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.graph}")
    print()
    print("Next steps:")
    print(f"  tools/enrich_elevations.py {args.graph}   # fill elevation data")
    print(f"  # review imported nodes in the in-app debug overlay")
    print(f"  tools/publish.py {args.graph} --role graph")


if __name__ == "__main__":
    main()
