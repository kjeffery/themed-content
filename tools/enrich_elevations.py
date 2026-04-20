#!/usr/bin/env python3
"""Enrich a graph JSON file with terrain elevations from USGS EPQS.

Usage:
    tools/enrich_elevations.py <graph.json> [--overwrite]

For every node missing `elevationMeters`, the script queries USGS for its
coordinate. For every edge missing `elevationGainMeters`/`elevationLossMeters`,
it samples points along the a→b line (linear lat/lng interpolation — under
1 km the error vs. great-circle is sub-millimeter) and sums the positive and
negative deltas to produce cumulative rise and drop.

Safe to re-run: nodes/edges that already have elevation data are skipped
unless `--overwrite` is passed.

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


def enrich_nodes(nodes: list[dict], overwrite: bool) -> None:
    to_fetch = [n for n in nodes if overwrite or n.get("elevationMeters") is None]
    print(f"nodes: {len(nodes)} total, {len(to_fetch)} need elevation")
    for i, node in enumerate(to_fetch, 1):
        coord = node["coord"]
        try:
            elevation = fetch_elevation_m(coord["latitude"], coord["longitude"])
        except Exception as err:
            print(f"  [{i}/{len(to_fetch)}] node {node['id']}: error ({err}); skipping")
            continue
        node["elevationMeters"] = round(elevation, 3)
        print(f"  [{i}/{len(to_fetch)}] node {node['id']}: {elevation:.2f}m")
        time.sleep(SLEEP_SECONDS)


def sample_points(
    a_lat: float, a_lng: float,
    b_lat: float, b_lng: float,
    count: int,
) -> list[tuple[float, float]]:
    # Linear interpolation in lat/lng space. At theme-park scale (<1 km)
    # this differs from great-circle by << 1 mm; not worth the trig.
    if count < 2:
        count = 2
    return [
        (a_lat + (b_lat - a_lat) * t / (count - 1),
         a_lng + (b_lng - a_lng) * t / (count - 1))
        for t in range(count)
    ]


def sample_count_for(meters: float) -> int:
    # ~1 sample every 10 m, bounded. Smallest park edges are ~20 m; longest
    # plausible are ~400 m. Range: 3 – 40 samples.
    return max(3, min(40, int(meters / 10) + 1))


def enrich_edges(graph: dict, overwrite: bool) -> None:
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    edges = graph.get("edges", [])
    to_fetch = [
        e for e in edges
        if overwrite
        or e.get("elevationGainMeters") is None
        or e.get("elevationLossMeters") is None
    ]
    print(f"edges: {len(edges)} total, {len(to_fetch)} need elevation profile")

    for i, edge in enumerate(to_fetch, 1):
        a = nodes_by_id.get(edge["a"])
        b = nodes_by_id.get(edge["b"])
        if a is None or b is None:
            print(f"  [{i}/{len(to_fetch)}] edge {edge['id']}: missing endpoint, skipping")
            continue

        samples = sample_points(
            a["coord"]["latitude"], a["coord"]["longitude"],
            b["coord"]["latitude"], b["coord"]["longitude"],
            sample_count_for(edge.get("meters", 0)),
        )

        elevations: list[float] = []
        failed = False
        for lat, lng in samples:
            try:
                elevations.append(fetch_elevation_m(lat, lng))
            except Exception as err:
                print(f"  [{i}/{len(to_fetch)}] edge {edge['id']}: error ({err}); skipping")
                failed = True
                break
            time.sleep(SLEEP_SECONDS)
        if failed or len(elevations) < 2:
            continue

        gain = 0.0
        loss = 0.0
        for e1, e2 in zip(elevations, elevations[1:]):
            delta = e2 - e1
            if delta > 0:
                gain += delta
            else:
                loss -= delta
        edge["elevationGainMeters"] = round(gain, 3)
        edge["elevationLossMeters"] = round(loss, 3)
        print(
            f"  [{i}/{len(to_fetch)}] edge {edge['id']}: "
            f"{len(samples)} samples, gain {gain:.2f}m loss {loss:.2f}m"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("graph", type=Path, help="path to graph.json")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="refetch even for nodes/edges that already have elevation data",
    )
    args = parser.parse_args()

    graph = json.loads(args.graph.read_text())
    enrich_nodes(graph["nodes"], overwrite=args.overwrite)
    enrich_edges(graph, overwrite=args.overwrite)
    args.graph.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.graph}")


if __name__ == "__main__":
    main()
