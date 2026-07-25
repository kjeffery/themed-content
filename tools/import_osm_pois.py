#!/usr/bin/env python3
"""Import OSM restrooms and shops into a graph JSON file.

Usage:
    tools/import_osm_pois.py <graph.json> [--dry-run] [--kinds restroom,shop]

Queries the Overpass API for `amenity=toilets` and `shop=*` elements clipped
to the Disneyland and Disney California Adventure park polygons, then merges
them into the graph's `pois` array with kind `restroom` / `shop`.

Idempotent — re-running updates names/coords for already-imported POIs
without duplicating. Matching happens in two tiers:

1. By id. New imports get a *deterministic* UUIDv5 derived from the OSM
   element (namespace UUID below + "osm-{kind}-{type}{id}"), so the same OSM
   element always maps to the same POI id. Plain string ids ("osm-…") are NOT
   valid — the app's POI.id is a UUID and the graph would fail to decode.
2. Legacy adoption by proximity. POIs imported before the UUIDv5 scheme carry
   random UUIDs with no OSM linkage, so a candidate that doesn't match by id
   adopts the nearest unclaimed existing POI of the same kind within
   ADOPT_RADIUS_METERS (exact-name matches are preferred). The existing POI's
   id is KEPT — other documents (poi_entrance_overrides.json) reference these
   UUIDs, so rewriting ids would orphan them.

Fields the importer doesn't manage — `elevationMeters` (filled by
enrich_elevations.py), `searchAliases`, `themeParksEntityID` — are preserved
on update. Imported POIs carry no `themeParksEntityID` (OSM has no link back
to themeparks.wiki) and no edges; connect them to the walkable surface via
the in-app hex painter if you want them routable.

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
import math
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Namespace for deterministic POI ids. Fixed forever: changing it would remint
# every OSM-derived id and orphan any cross-references. Generated once via
# uuid4 when the UUIDv5 scheme was introduced.
OSM_POI_NAMESPACE = uuid.UUID("6c1f24a8-3ce4-4bb9-9e05-c6f0b8a7d43e")

# How far (meters) a candidate may sit from a legacy POI of the same kind and
# still adopt it. Tight on purpose: Main Street shops sit a few meters apart,
# and a too-generous radius would cross-match neighbors. Exact-name matches
# get a looser leash (names disambiguate).
ADOPT_RADIUS_METERS = 8.0
ADOPT_RADIUS_NAMED_METERS = 25.0

# Theme-park boundary relations on OSM. Found via:
#   way[tourism=theme_park](around the resort).
PARKS = {
    # graph `park` raw value -> (OSM relation id, human label for logging)
    "disneyland": (5586855, "Disneyland"),
    "california-adventure": (15626312, "Disney California Adventure"),
}

# Tag values we never want to import. Closed storefronts are noise.
#
# NOTE: access=private is deliberately NOT here anymore. Cast/backstage
# facilities are imported keep-and-mark style: they land in the catalog
# with `"access": "private"` (see PRIVATE_ACCESS_VALUES) so the hex
# painter can see and manage them, while the app's guest surfaces filter
# them out via `AppConfig.graph`. Culling them at import just meant OSM
# re-offered them forever.
EXCLUDED_TAG_VALUES = {
    ("shop", "vacant"),
}

# OSM `access` values that mark a POI as cast/backstage rather than
# guest-facing. Written as `"access": "private"` on NEW imports only —
# `update_in_place` never touches the field, so a hand-set (or hand-
# cleared) designation survives every re-import.
PRIVATE_ACCESS_VALUES = {"private", "employees", "no", "permit"}


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
    """Deterministic UUID for an OSM element.

    OSM ids are stable across edits but not across element types — a node and
    a way can share an id — so the seed includes the type. UUIDv5 keeps the
    result a valid POI.id (the app decodes ids as UUIDs) while staying stable
    across re-runs. Uppercased to match the graph's existing id formatting.
    """
    seed = f"osm-{kind}-{element['type']}{element['id']}"
    return str(uuid.uuid5(OSM_POI_NAMESPACE, seed)).upper()


def make_poi(element: dict, kind: str, park_raw: str) -> dict:
    lat, lon = coord_for(element)
    poi = {
        "id": stable_id(element, kind),
        "name": display_name(element, kind),
        "kind": kind,
        "coord": {"latitude": lat, "longitude": lon},
        "park": park_raw,
    }
    if element.get("tags", {}).get("access") in PRIVATE_ACCESS_VALUES:
        poi["access"] = "private"
    return poi


def distance_meters(a: dict, b: dict) -> float:
    """Equirectangular distance — plenty accurate at park scale."""
    lat1, lon1 = a["latitude"], a["longitude"]
    lat2, lon2 = b["latitude"], b["longitude"]
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * meters_per_deg_lat
    dx = (lon2 - lon1) * meters_per_deg_lon
    return math.hypot(dx, dy)


def update_in_place(existing: dict, candidate: dict) -> bool:
    """Overwrite only the importer-managed fields; keep everything else
    (elevationMeters, searchAliases, themeParksEntityID, `access`, the
    existing id). `access` in particular is hand-owned after first import:
    OSM tags seed it on brand-new POIs only, so a human marking a POI
    private (or clearing a wrong mark) is never overwritten by a re-run.
    Returns True when anything actually changed."""
    merged = dict(existing)
    merged["name"] = candidate["name"]
    merged["coord"] = candidate["coord"]
    merged["park"] = candidate["park"]
    merged["kind"] = candidate["kind"]
    if merged != existing:
        existing.clear()
        existing.update(merged)
        return True
    return False


def adopt_legacy(candidate: dict, pois: list[dict], claimed: set[str]) -> dict | None:
    """Find a pre-UUIDv5 POI this candidate corresponds to.

    Same kind, unclaimed, within ADOPT_RADIUS_METERS — or within the looser
    ADOPT_RADIUS_NAMED_METERS when the names match exactly. Nearest wins.
    """
    best = None
    best_dist = math.inf
    for poi in pois:
        if poi["kind"] != candidate["kind"] or poi["id"] in claimed:
            continue
        dist = distance_meters(poi["coord"], candidate["coord"])
        limit = (
            ADOPT_RADIUS_NAMED_METERS
            if poi.get("name") == candidate["name"]
            else ADOPT_RADIUS_METERS
        )
        if dist <= limit and dist < best_dist:
            best = poi
            best_dist = dist
    return best


def merge_pois(graph: dict, candidates: list[dict]) -> tuple[int, int]:
    """Merge OSM-sourced POIs into the graph in-place.

    Returns (added, updated). Manually-authored POIs are never touched: a
    candidate can only update the POI with its own deterministic id or a
    same-kind legacy POI adopted by proximity.
    """
    pois = graph["pois"]
    by_id = {p["id"]: p for p in pois}
    claimed: set[str] = set()
    added = 0
    updated = 0
    for candidate in candidates:
        existing = by_id.get(candidate["id"])
        if existing is None:
            existing = adopt_legacy(candidate, pois, claimed)
        if existing is None:
            pois.append(candidate)
            by_id[candidate["id"]] = candidate
            claimed.add(candidate["id"])
            added += 1
            continue
        claimed.add(existing["id"])
        if update_in_place(existing, candidate):
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
    if "pois" not in graph:
        raise SystemExit(
            "graph file has no 'pois' array — this tool requires the "
            "formatVersion 2 catalog schema"
        )

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
            candidates.append(make_poi(el, kind, park_raw))
            kept += 1
        print(
            f"  {label}: {kept} kept, "
            f"{skipped_excluded} excluded by tag, "
            f"{skipped_no_coord} missing coords",
            file=sys.stderr,
        )
        # Be a polite Overpass citizen — small gap between the two queries.
        time.sleep(1.0)

    added, updated = merge_pois(graph, candidates)
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
    print(f"  # review imported POIs in the in-app debug overlay")
    print(f"  tools/publish.py {args.graph} --role graph")


if __name__ == "__main__":
    main()
