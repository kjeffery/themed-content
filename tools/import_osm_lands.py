#!/usr/bin/env python3
"""Import OpenStreetMap themed-land polygons into themed-content/lands.json.

Usage:
    tools/import_osm_lands.py [--output lands.json] [--dry-run]

Queries the Overpass API for `tourism=theme_park` and `leisure=park` ways
inside the Disneyland and Disney California Adventure park polygons, then
filters to the curated list of known land names below and writes them as
a `LandsDocument`-shaped JSON file the app can decode.

The allowlist exists because OSM tags inside the parks include both the
sub-park lands we want (Frontierland, Cars Land, etc.) and miscellaneous
greenery (planters, ornamental ponds). Anything matching a known land
name is kept; anything else is reported under "extra polygons found in
park" so authors can decide whether to add it to the allowlist.

Idempotent — writes the whole file every run, sorted by park then name
so re-imports produce minimal diffs.

No auth required. Overpass is rate-limited; this script makes one
request per park (two total).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Same OSM relation IDs the POI importer uses for park-area clipping.
PARKS = {
    # graph `park` raw value -> (OSM relation id, human label for logging)
    "disneyland": (5586855, "Disneyland"),
    "california-adventure": (15626312, "Disney California Adventure"),
}

# Curated list of land names per park. Matching is case- and punctuation-
# insensitive (see `normalize_name`) so "Star Wars: Galaxy's Edge" matches
# whether OSM has it as "Star Wars: Galaxy's Edge" or "Star Wars Galaxys Edge".
KNOWN_LANDS = {
    "disneyland": {
        "Main Street, U.S.A.",
        "Adventureland",
        "Frontierland",
        "Critter Country",         # also tagged "Bayou Country" since 2024 rename in some maps
        "Bayou Country",
        "New Orleans Square",
        "Fantasyland",
        "Mickey's Toontown",
        "Tomorrowland",
        "Star Wars: Galaxy's Edge",
    },
    "california-adventure": {
        "Buena Vista Street",
        "Hollywood Land",
        "Pacific Wharf",           # renamed area; OSM may have either
        "San Fransokyo Square",
        "Pixar Pier",
        "Paradise Gardens Park",
        "Cars Land",
        "Grizzly Peak",
        "Avengers Campus",
    },
}


def normalize_name(s: str) -> str:
    """Case-insensitive, punctuation-loose name comparison."""
    s = s.lower().replace("'", "").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def overpass_query(rel_id: int, max_attempts: int = 3) -> list[dict]:
    """Fetch named polygons (ways + multipolygon relations) of any kind
    inside the given park relation. We don't filter on `leisure` /
    `tourism` here because OSM tags Disneyland's lands inconsistently —
    some are `leisure=park`, some `tourism=theme_park`, some
    `boundary=themed_area`, and a few have no category tag at all. The
    name-based allowlist filter in `main` handles the noise this lets in.

    Retries on Overpass 5xx (the server is sometimes overloaded). Each
    retry doubles the backoff up to ~30s.
    """
    query = f"""
[out:json][timeout:90];
rel({rel_id})->.park;
.park map_to_area->.parkArea;
(
  way["name"](area.parkArea);
  relation["type"="multipolygon"]["name"](area.parkArea);
);
out geom tags;
"""
    body = urllib.parse.urlencode({"data": query}).encode()
    backoff = 4.0
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            OVERPASS_URL,
            data=body,
            headers={"User-Agent": "themed-import-osm-lands/1.0 (github.com/kjeffery/themed-content)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                payload = json.loads(response.read())
            return payload.get("elements", [])
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code >= 500 and attempt < max_attempts:
                print(
                    f"    Overpass {err.code}; retrying in {backoff:.0f}s "
                    f"(attempt {attempt}/{max_attempts})…",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            raise
    raise last_error or RuntimeError("Overpass query failed without an error")


def polygon_from_element(element: dict) -> list[dict]:
    """Convert an Overpass element (way or multipolygon relation) into
    the `[{latitude, longitude}, ...]` shape the Swift `Land` decoder
    expects.

    For ways: returns the way's node geometry. For relations: stitches
    the outer ring(s) together — we drop `inner` members (holes), which
    is a deliberate v1 limitation. Holes in themed lands are rare and
    the rendered visual impact is minor (a slightly larger filled area).

    Strips a duplicate closing node if present — MapKit's `MapPolygon`
    auto-closes, and the explicit duplicate would render as a zero-length
    edge.
    """
    raw_points: list[dict] = []
    if element["type"] == "way":
        raw_points = element.get("geometry") or []
    elif element["type"] == "relation":
        # Concatenate outer members' geometries. Real multipolygon
        # handling would assemble rings by matching endpoints, but for
        # a single-ring named land that's overkill — outers are usually
        # already ordered into one closed ring by OSM convention.
        for member in element.get("members", []):
            if member.get("role") == "outer" and member.get("geometry"):
                raw_points.extend(member["geometry"])
    points = [{"latitude": p["lat"], "longitude": p["lon"]} for p in raw_points]
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    return points


def slug_for(name: str, park_raw: str) -> str:
    """Stable, human-readable id used in `lands.json` and downstream
    Swift references. Park suffix disambiguates a land name that might
    appear in multiple parks (e.g. "Paradise" could exist in both)."""
    slug_name = normalize_name(name).replace(" ", "-")
    park_suffix = "dlr" if park_raw == "disneyland" else "dca"
    return f"{slug_name}-{park_suffix}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "lands.json",
        help="output path (default: themed-content/lands.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without writing the file",
    )
    args = parser.parse_args()

    lands: list[dict] = []
    for park_raw, (rel_id, label) in PARKS.items():
        print(f"querying Overpass for lands in {label} (rel {rel_id})…", file=sys.stderr)
        elements = overpass_query(rel_id)

        wanted_normalized = {normalize_name(n) for n in KNOWN_LANDS[park_raw]}
        matched: list[tuple[str, dict]] = []
        extras: list[str] = []
        for el in elements:
            name = el.get("tags", {}).get("name")
            if not name:
                continue
            polygon = polygon_from_element(el)
            if len(polygon) < 3:
                continue  # not a polygon
            if normalize_name(name) in wanted_normalized:
                matched.append((name, el))
            else:
                extras.append(name)

        print(
            f"  {label}: {len(matched)} matched, "
            f"{len(extras)} extras (not in allowlist)",
            file=sys.stderr,
        )
        for extra in sorted(set(extras))[:10]:
            print(f"    extra: {extra!r}", file=sys.stderr)
        if len(set(extras)) > 10:
            print(f"    ... and {len(set(extras)) - 10} more", file=sys.stderr)

        seen_normalized: set[str] = set()
        for name, element in matched:
            key = normalize_name(name)
            if key in seen_normalized:
                # OSM occasionally has duplicate polygons (an outer relation
                # plus a separate way for the same land). Keep the first.
                continue
            seen_normalized.add(key)
            lands.append({
                "id": slug_for(name, park_raw),
                "name": name,
                "park": park_raw,
                "polygon": polygon_from_element(element),
            })

        # Polite gap between the two queries.
        time.sleep(1.0)

    # Sort for stable diffs across runs.
    lands.sort(key=lambda l: (l["park"], l["name"]))

    document = {
        "formatVersion": 1,
        "destination": "Disneyland Resort",
        "lands": lands,
    }

    print(f"\nTotal lands: {len(lands)}")
    by_park: dict[str, int] = {}
    for l in lands:
        by_park[l["park"]] = by_park.get(l["park"], 0) + 1
    for park_raw, count in sorted(by_park.items()):
        expected = len(KNOWN_LANDS[park_raw])
        print(f"  {park_raw}: {count} (allowlist has {expected})")

    # Report missing lands so authors know whether to update the allowlist
    # or chase down an OSM tagging fix.
    for park_raw, expected_set in KNOWN_LANDS.items():
        found_names = {l["name"] for l in lands if l["park"] == park_raw}
        found_normalized = {normalize_name(n) for n in found_names}
        missing = [
            n for n in expected_set
            if normalize_name(n) not in found_normalized
        ]
        if missing:
            print(f"  missing in {park_raw}: {sorted(missing)}")

    if args.dry_run:
        print("\n(dry run — file not written)")
        return

    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"\nwrote {args.output}")
    print()
    print("Next steps:")
    print(f"  tools/publish.py {args.output} --role lands")


if __name__ == "__main__":
    main()
