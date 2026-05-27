#!/usr/bin/env python3
"""Enrich a POI catalog JSON file (the migrated `graph.json`) with USGS
terrain elevations, per POI.

Usage:
    tools/enrich_elevations.py <graph.json> [--overwrite]

For every POI missing `elevationMeters`, the script queries USGS for its
coordinate and writes the value back. Safe to re-run: POIs that already
have elevation data are skipped unless `--overwrite` is passed.

Schema note (Path-B, #24): the JSON's top-level array is `pois` (with
UUID `id`s). The legacy `nodes`-keyed schema (pre-#24) is no longer
supported; if you have an old file, run `tools/migrate_graph_to_pois.py`
first. Edge enrichment is gone — walking topology lives on the hex grid
now (`tools/enrich_hex_elevations.py` enriches per cell).

Free, no auth. USGS doesn't publish a hard rate limit; the script throttles
gently at ~7 req/s.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
SLEEP_SECONDS = 0.15


class EPQSError(RuntimeError):
    pass


def fetch_elevation_m(lat: float, lng: float, attempts: int = 3) -> float:
    params = urllib.parse.urlencode({
        "x": lng,
        "y": lat,
        "units": "Meters",
        "wkid": 4326,
        "includeDate": "false",
    })
    url = f"{EPQS_URL}?{params}"
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                body = json.loads(response.read())
            value = body.get("value")
            if value is None or value == "-1000000":
                raise EPQSError(f"no elevation at ({lat}, {lng})")
            return float(value)
        except EPQSError:
            raise
        except Exception as err:
            last_err = err
            time.sleep(0.5 * (attempt + 1))  # gentle backoff
    raise EPQSError(f"all {attempts} attempts failed at ({lat}, {lng}): {last_err}")


def enrich_pois(pois: list[dict], overwrite: bool) -> None:
    to_fetch = [p for p in pois if overwrite or p.get("elevationMeters") is None]
    print(f"POIs: {len(pois)} total, {len(to_fetch)} need elevation")
    for i, poi in enumerate(to_fetch, 1):
        coord = poi["coord"]
        label = poi.get("name") or poi.get("id", "(no id)")
        try:
            elevation = fetch_elevation_m(coord["latitude"], coord["longitude"])
        except Exception as err:
            print(f"  [{i}/{len(to_fetch)}] {label}: error ({err}); skipping")
            continue
        poi["elevationMeters"] = round(elevation, 3)
        print(f"  [{i}/{len(to_fetch)}] {label}: {elevation:.2f}m")
        time.sleep(SLEEP_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("graph", type=Path, help="path to graph.json")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="refetch even for POIs that already have elevation data",
    )
    args = parser.parse_args()

    graph = json.loads(args.graph.read_text())
    if "pois" not in graph:
        if "nodes" in graph:
            raise SystemExit(
                f"{args.graph} is the legacy node-graph schema (has `nodes`, "
                "not `pois`). Run tools/migrate_graph_to_pois.py first to "
                "upgrade it to the Path-B catalog format."
            )
        raise SystemExit(
            f"{args.graph} doesn't look like a POI catalog "
            "(missing top-level `pois` array)."
        )
    enrich_pois(graph["pois"], overwrite=args.overwrite)
    args.graph.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.graph}")


if __name__ == "__main__":
    main()
