#!/usr/bin/env python3
"""Enrich a graph JSON file with terrain elevations from USGS EPQS.

Usage:
    tools/enrich_elevations.py <graph.json> [--overwrite]

For every node in the graph that's missing an `elevationMeters` field, this
script queries the USGS Elevation Point Query Service (free, no auth, US only)
and writes the result back. Safe to re-run — nodes that already have an
elevation are skipped unless `--overwrite` is passed.

The script edits the file in place and preserves the Swift-compatible format:
sorted keys, 2-space indent, trailing newline.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
# Gentle throttle — USGS doesn't publish a hard rate limit but is a
# public service. Tuned for ~200 nodes completing in under a minute.
SLEEP_SECONDS = 0.15


class EPQSError(RuntimeError):
    pass


def fetch_elevation_m(lat: float, lng: float) -> float:
    params = urllib.parse.urlencode({
        "x": lng,
        "y": lat,
        "units": "Meters",
        "wkid": 4326,
        "includeDate": "false",
    })
    url = f"{EPQS_URL}?{params}"
    with urllib.request.urlopen(url, timeout=10) as response:
        body = json.loads(response.read())
    value = body.get("value")
    if value is None or value == "-1000000":  # USGS sentinel for "no data"
        raise EPQSError(f"no elevation at ({lat}, {lng})")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("graph", type=Path, help="path to graph.json")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="refetch elevations even for nodes that already have one",
    )
    args = parser.parse_args()

    graph = json.loads(args.graph.read_text())
    nodes = graph.get("nodes", [])

    to_fetch = [
        n for n in nodes
        if args.overwrite or n.get("elevationMeters") is None
    ]
    print(f"{len(nodes)} nodes total, {len(to_fetch)} need elevation")

    if not to_fetch:
        return

    for i, node in enumerate(to_fetch, 1):
        lat = node["coord"]["latitude"]
        lng = node["coord"]["longitude"]
        try:
            elevation = fetch_elevation_m(lat, lng)
        except (EPQSError, Exception) as err:
            print(f"  [{i}/{len(to_fetch)}] {node['id']}: error ({err}); skipping")
            continue
        node["elevationMeters"] = round(elevation, 3)
        print(f"  [{i}/{len(to_fetch)}] {node['id']}: {elevation:.2f}m")
        time.sleep(SLEEP_SECONDS)

    # Preserve Swift-compatible formatting.
    args.graph.write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {args.graph}")


if __name__ == "__main__":
    main()
