#!/usr/bin/env python3
"""Enrich a `hexgrid.json` file with USGS terrain elevations, per cell.

Usage:
    tools/enrich_hex_elevations.py <hexgrid.json> [--overwrite] [--concurrency 8]

For every cell missing `elevationMeters`, the script computes the cell's
center in world coordinates from `grid.origin` + the cell's axial (q, r),
queries USGS EPQS for that point, and writes the value back. Per-cell
work because the hex substrate replaces the old node/edge graph (where
edge profiles were sampled along chords) — every cell has its own
elevation now.

Idempotent. Re-running skips cells that already carry an elevation
unless `--overwrite` is passed.

Staleness model:
- Cell centers are pure functions of `origin.lat/lng/hexSizeMeters/
  orientation` and the cell's `(q, r)`. None of those change at runtime,
  so once a cell is enriched the value is permanent until you migrate
  the grid (apothem change, origin shift). On those migrations: re-run
  with `--overwrite`. USGS DEM revisions don't matter at theme-park
  spatial precision.

Rate limiting:
- USGS EPQS has no advertised hard limit; this script defaults to 8
  concurrent requests with a small per-request sleep on the worker so
  we don't pin the service. At Disneyland scale (~10k cells) the wall-
  clock cost is dominated by network latency: expect ~3-5 minutes with
  concurrency=8, ~20-30 minutes serial.

No auth required.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hexgrid_format  # noqa: E402

EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
EARTH_RADIUS_METERS = 6_371_000.0


class EPQSError(RuntimeError):
    pass


# Mirrors `GridOrigin.center(of:)` in themed/Routing/HexGrid.swift —
# changes there must be reflected here, otherwise enriched elevations
# drift from what the runtime computes for the same cell. The two
# branches cover the two `Orientation` cases (flatTop / pointyTop).
def cell_center(origin: dict, q: int, r: int) -> tuple[float, float]:
    apothem = origin["hexSizeMeters"]
    outer_radius = apothem * 2.0 / math.sqrt(3.0)
    orientation = origin.get("orientation", "flatTop")
    if orientation == "flatTop":
        dx = outer_radius * 1.5 * q
        dy = outer_radius * math.sqrt(3.0) * (r + q / 2.0)
    elif orientation == "pointyTop":
        dx = outer_radius * math.sqrt(3.0) * (q + r / 2.0)
        dy = outer_radius * 1.5 * r
    else:
        raise ValueError(f"unknown orientation: {orientation!r}")
    phi0 = math.radians(origin["latitude"])
    lat = origin["latitude"] + math.degrees(dy / EARTH_RADIUS_METERS)
    lng = origin["longitude"] + math.degrees(
        dx / (EARTH_RADIUS_METERS * math.cos(phi0))
    )
    return lat, lng


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


def enrich_cells(
    origin: dict,
    cells: list[dict],
    overwrite: bool,
    concurrency: int,
) -> tuple[int, int, int]:
    """Fill `elevationMeters` on the normalized `cells` list in place.

    `cells` is the format-agnostic list from `hexgrid_format.load_cells`:
    each dict has `q`, `r`, `elevationMeters` (float | None). Returns
    (filled, skipped_existing, failed).
    """
    todo: list[tuple[int, dict]] = [
        (i, c) for i, c in enumerate(cells)
        if overwrite or c.get("elevationMeters") is None
    ]
    skipped = len(cells) - len(todo)
    print(
        f"cells: {len(cells)} total, {len(todo)} need elevation, "
        f"{skipped} already enriched"
    )
    if not todo:
        return (0, skipped, 0)

    # Pre-compute world coords for each todo cell so the worker pool
    # spends its time on network, not trig.
    targets = [
        (idx, cell_center(origin, c["q"], c["r"]))
        for idx, c in todo
    ]

    filled = 0
    failed = 0
    completed = 0
    total = len(todo)

    def worker(idx: int, lat: float, lng: float) -> tuple[int, float | None, str | None]:
        try:
            return (idx, fetch_elevation_m(lat, lng), None)
        except Exception as err:
            return (idx, None, str(err))

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(worker, idx, lat, lng)
            for idx, (lat, lng) in targets
        ]
        for fut in concurrent.futures.as_completed(futures):
            idx, value, err = fut.result()
            completed += 1
            if value is None:
                failed += 1
                # Don't spam — print the first failure verbatim, then
                # collapse the rest into a tally so a transient EPQS
                # outage doesn't drown the console.
                if failed <= 3:
                    cell = cells[idx]
                    print(
                        f"  [{completed}/{total}] cell ({cell['q']}, {cell['r']}): "
                        f"error ({err}); skipping"
                    )
                continue
            cells[idx]["elevationMeters"] = round(value, 3)
            filled += 1
            # Progress every 100 cells so a long run doesn't look dead.
            if completed % 100 == 0 or completed == total:
                print(f"  [{completed}/{total}] filled {filled}, failed {failed}")

    if failed > 3:
        print(f"  (+ {failed - 3} more failures suppressed)")
    return (filled, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("hexgrid", type=Path, help="path to hexgrid.json")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="refetch even for cells that already have elevation data",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="parallel EPQS requests (default 8)",
    )
    args = parser.parse_args()

    grid = json.loads(args.hexgrid.read_text())
    if "origin" not in grid or "q" not in grid:
        raise SystemExit(
            f"{args.hexgrid} doesn't look like a hexgrid JSON "
            "(missing `origin` or `q` column)"
        )
    cells = hexgrid_format.load_cells(grid)

    start = time.monotonic()
    filled, skipped, failed = enrich_cells(
        grid["origin"], cells,
        overwrite=args.overwrite, concurrency=max(1, args.concurrency)
    )
    elapsed = time.monotonic() - start

    # Only write when something changed — keeps diffs clean on no-ops.
    if filled > 0:
        out = hexgrid_format.build_grid(grid["destination"], grid["origin"], cells)
        args.hexgrid.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(
            f"wrote {args.hexgrid} (filled {filled}, skipped {skipped}, "
            f"failed {failed}) in {elapsed:.1f}s"
        )
    else:
        print(
            f"no changes (skipped {skipped}, failed {failed}) in {elapsed:.1f}s"
        )
        if failed > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
