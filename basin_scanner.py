#!/usr/bin/env python3
"""
Annual basin-wide screening for sudden tree and crop decline around the
Mediterranean, sized for GitHub's free runners.

The weekly regional scan (xylella_scanner.py) looks at a few olive districts at
10 m. This script applies the same rules to the whole olive belt in basin.json
at 100 m, once a year, after the July-August season it compares. One 100 m cell
covers about a hectare (roughly 100 traditional olive trees), so only
landscape-scale decline shows: the kind of grove collapse seen in the Salento,
not single trees. Results are summarised in 5 km squares for the overview map.
A square that lights up is a reason to look closer, for example by adding a
10 m region to regions.json, never evidence of Xylella by itself.

Commands
    python basin_scanner.py --plan 20               # how many batches are needed
    python basin_scanner.py --batch 3 --of 20 --budget-min 320 --write-dir basin_out
    python basin_scanner.py --merge                 # build data/basin/hotspots.geojson

Work is done cell by cell and saved after each one, so a run that hits its time
budget simply continues in the next scheduled run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "basin.json"
OUT = ROOT / "data" / "basin"
CELLS = OUT / "cells"

RESOLUTION = 100                  # metres per cell
FIRST_SEASON = 2022               # first season judged (2020-2021 serve as baselines)
MIN_AREA_M2 = 30_000              # 3 ha, i.e. 3 cells
BLOCK_PX = 50                     # 5 km summary squares
MIN_BLOCK_HA = 3                  # squares with less flagged land are not shown
MIN_LAND_SHARE = 0.02             # skip cells that are mostly sea, desert or towns
BAND_RESAMPLING = {"B04": "average", "B08": "average", "SCL": "nearest"}


# --------------------------------------------------------------------------
# Cells and bookkeeping (standard library only)
# --------------------------------------------------------------------------
def cell_id(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def all_cells() -> list[tuple[str, list[float]]]:
    domains = json.loads(CONFIG.read_text())["domains"]
    seen = {}
    for d in domains:
        w, s, e, n = d["bbox"]
        for lat in range(math.floor(s), math.ceil(n)):
            for lon in range(math.floor(w), math.ceil(e)):
                seen[cell_id(lat, lon)] = [lon, lat, lon + 1, lat + 1]
    return sorted(seen.items())


def latest_complete_season(today: dt.date) -> int:
    return today.year if today >= dt.date(today.year, 9, 1) else today.year - 1


def params_hash() -> str:
    import xylella_scanner as xs   # needs numpy
    blob = json.dumps({"rules": xs.PARAMS, "resolution": RESOLUTION, "first": FIRST_SEASON,
                       "min_area": MIN_AREA_M2, "block": BLOCK_PX, "land": MIN_LAND_SHARE,
                       "resampling": BAND_RESAMPLING}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_meta(cid: str):
    p = CELLS / f"{cid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def pending_cells(today: dt.date, phash: str) -> list[tuple[str, list[float]]]:
    latest = latest_complete_season(today)
    todo = []
    for cid, bbox in all_cells():
        meta = read_meta(cid)
        if not meta or meta.get("params_hash") != phash:
            todo.append((cid, bbox))
        elif not meta.get("skip") and meta.get("done_through", 0) < latest:
            todo.append((cid, bbox))
    return todo


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Screening one cell
# --------------------------------------------------------------------------
def summarize_blocks(status, first, transform, crs: str, latest: int, cid: str) -> list:
    """5 km squares (GeoJSON features in EPSG:4326) with flagged hectares by status."""
    import numpy as np
    from rasterio.warp import transform as warp

    px_ha = RESOLUTION * RESOLUTION / 10_000
    h, w = status.shape
    nby, nbx = math.ceil(h / BLOCK_PX), math.ceil(w / BLOCK_PX)
    pad = np.zeros((nby * BLOCK_PX, nbx * BLOCK_PX), np.uint8)
    pad[:h, :w] = status
    blocks = pad.reshape(nby, BLOCK_PX, nbx, BLOCK_PX).transpose(0, 2, 1, 3)
    counts = {code: (blocks == code).sum(axis=(2, 3)) for code in (1, 2, 3)}
    fpad = np.zeros_like(pad, dtype=np.uint16)
    fpad[:h, :w] = np.where(status > 0, first, 0)
    fblocks = fpad.reshape(nby, BLOCK_PX, nbx, BLOCK_PX).transpose(0, 2, 1, 3)

    a, _, c, _, e, f = list(transform)[:6]
    feats = []
    total = counts[1] + counts[2] + counts[3]
    for by, bx in zip(*np.nonzero(total * px_ha >= MIN_BLOCK_HA)):
        x0, x1 = c + bx * BLOCK_PX * a, c + min((bx + 1) * BLOCK_PX, w) * a
        y0, y1 = f + by * BLOCK_PX * e, f + min((by + 1) * BLOCK_PX, h) * e
        lons, lats = warp(crs, "EPSG:4326", [x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0])
        new_ha, again_ha, earlier_ha = (round(float(counts[k][by, bx]) * px_ha, 1) for k in (1, 2, 3))
        fb = fblocks[by, bx]
        status_name = ("new" if new_ha >= MIN_BLOCK_HA else
                       "continuing" if again_ha >= MIN_BLOCK_HA else "earlier")
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": [[[round(x, 4), round(y, 4)] for x, y in zip(lons, lats)]]},
            "properties": {"cell": cid, "status": status_name, "new_ha": new_ha, "again_ha": again_ha,
                           "earlier_ha": earlier_ha, "first_season": int(fb[fb > 0].min()) if (fb > 0).any() else None,
                           "latest_season": latest},
        })
    return feats


def process_cell(cid: str, bbox, latest: int, phash: str, write_dir: Path) -> str:
    import numpy as np
    import xylella_scanner as xs

    t0 = time.time()
    gbox = xs.region_geobox(bbox, resolution=RESOLUTION)
    shape = list(gbox.shape)
    meta = read_meta(cid)
    state_file = CELLS / f"{cid}.npz"
    reuse = (meta is not None and meta.get("params_hash") == phash and not meta.get("skip")
             and state_file.exists())

    catalog = xs.open_catalog()
    landmask, mask_info = xs.build_landmask(catalog, gbox, bbox, cid, builtup_buffer_px=0, resampling="mode")
    land_share = float(landmask.mean())
    if land_share < MIN_LAND_SHARE:
        write_atomic(write_dir / f"{cid}.hotspots.json", "[]\n")
        write_atomic(write_dir / f"{cid}.json", json.dumps(
            {"cell": cid, "params_hash": phash, "skip": True, "land_share": round(land_share, 3),
             "masks": mask_info, "checked": dt.date.today().isoformat()}, indent=1))
        return f"skipped (land share {land_share:.1%})"

    if reuse:
        with np.load(state_file) as z:
            flagged, recovered = z["flagged"].copy(), z["recovered"].copy()
        evaluate = list(range(max(FIRST_SEASON, meta["done_through"]), latest + 1))
    else:
        flagged = np.zeros(shape, np.uint32)
        recovered = np.zeros(shape, np.uint32)
        evaluate = list(range(FIRST_SEASON, latest + 1))

    comps, dates = {}, {}
    for year in range(min(evaluate) - 2, latest + 1):
        try:
            comps[year], dates[year] = xs.load_season(catalog, gbox, bbox, year, resampling=BAND_RESAMPLING)
        except Exception as exc:
            print(f"  {cid} {year}: {exc.__class__.__name__}: {exc}", flush=True)
            comps[year], dates[year] = None, 0
    if comps.get(latest) is None or comps.get(latest - 1) is None:
        raise RuntimeError("no usable imagery for the latest comparison")

    burned, mask_info["burned_area"] = xs.load_burned(catalog, gbox, bbox, evaluate)
    min_px = max(1, math.ceil(MIN_AREA_M2 / RESOLUTION ** 2))
    xs.update_history(comps, evaluate, landmask, flagged, recovered, min_px=min_px, burned=burned)
    status, first = xs.classify(flagged, recovered, latest)
    feats = summarize_blocks(status, first, gbox.transform, str(gbox.crs), latest, cid)
    stats = xs.season_stats(flagged, recovered, range(FIRST_SEASON, latest + 1),
                            px_ha=RESOLUTION * RESOLUTION / 10_000)

    write_dir.mkdir(parents=True, exist_ok=True)
    tmp = write_dir / f"{cid}.npz.tmp.npz"
    np.savez_compressed(tmp, flagged=flagged, recovered=recovered)
    os.replace(tmp, write_dir / f"{cid}.npz")
    write_atomic(write_dir / f"{cid}.hotspots.json", json.dumps(feats) + "\n")
    write_atomic(write_dir / f"{cid}.json", json.dumps({
        "cell": cid, "bbox": bbox, "params_hash": phash, "skip": False, "done_through": latest,
        "land_share": round(land_share, 3), "dates": {str(k): v for k, v in dates.items()},
        "seasons": {str(k): v for k, v in stats.items()}, "masks": mask_info,
        "checked": dt.date.today().isoformat(), "seconds": round(time.time() - t0)}, indent=1))
    return f"{len(feats)} squares flagged in {time.time() - t0:.0f} s"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_plan(max_batches: int, today: dt.date) -> None:
    todo = pending_cells(today, params_hash())
    n = min(max_batches, len(todo))
    print(json.dumps({"pending": len(todo), "count": n, "batches": list(range(n))}))


def cmd_batch(index: int, of: int, budget_min: float, write_dir: Path, today: dt.date) -> int:
    phash = params_hash()
    latest = latest_complete_season(today)
    mine = pending_cells(today, phash)[index::of]
    print(f"Batch {index + 1}/{of}: {len(mine)} cells, season {latest}, budget {budget_min:.0f} min", flush=True)
    start, slowest, failures = time.time(), 0.0, 0
    for i, (cid, bbox) in enumerate(mine, 1):
        elapsed = time.time() - start
        if elapsed + 1.5 * slowest > budget_min * 60:
            print(f"Time budget reached after {i - 1} cells; the rest continue next run.")
            break
        t = time.time()
        try:
            print(f"[{i}/{len(mine)}] {cid}: {process_cell(cid, bbox, latest, phash, write_dir)}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"[{i}/{len(mine)}] {cid}: FAILED {exc.__class__.__name__}: {exc}", flush=True)
        slowest = max(slowest, time.time() - t)
    return 1 if failures and failures == len(mine) else 0


def cmd_merge(today: dt.date) -> None:
    cells = all_cells()
    feats, done, skipped, behind, totals = [], 0, 0, 0, {"new": 0.0, "continuing": 0.0, "earlier": 0.0}
    latest = latest_complete_season(today)
    seasons_seen = set()
    for cid, _ in cells:
        meta = read_meta(cid)
        if meta and meta.get("skip"):
            skipped += 1
            continue
        if not meta:
            behind += 1
            continue
        if meta.get("done_through", 0) < latest:
            behind += 1
        else:
            done += 1
        seasons_seen.add(meta.get("done_through"))
        hp = CELLS / f"{cid}.hotspots.json"
        if hp.exists():
            for f in json.loads(hp.read_text()):
                feats.append(f)
                p = f["properties"]
                totals["new"] += p["new_ha"]
                totals["continuing"] += p["again_ha"]
                totals["earlier"] += p["earlier_ha"]
    write_atomic(OUT / "hotspots.geojson", json.dumps({"type": "FeatureCollection", "features": feats}) + "\n")
    summary = {
        "season": latest, "resolution_m": RESOLUTION, "square_km": BLOCK_PX * RESOLUTION / 1000,
        "cells_total": len(cells), "cells_done": done, "cells_skipped": skipped, "cells_pending": behind,
        "seasons_in_data": sorted(s for s in seasons_seen if s),
        "flagged_ha": {k: round(v) for k, v in totals.items()}, "squares": len(feats),
        "merged": today.isoformat(),
    }
    write_atomic(OUT / "summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Annual basin-wide decline screening")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", type=int, metavar="MAX_BATCHES")
    g.add_argument("--batch", type=int)
    g.add_argument("--merge", action="store_true")
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--budget-min", type=float, default=320)
    ap.add_argument("--write-dir", default=str(CELLS))
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if a.plan is not None:
        cmd_plan(a.plan, today)
    elif a.batch is not None:
        return cmd_batch(a.batch, a.of, a.budget_min, Path(a.write_dir), today)
    else:
        cmd_merge(today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
