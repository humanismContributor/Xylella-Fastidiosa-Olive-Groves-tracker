#!/usr/bin/env python3
"""
Xylella decline scanner: Sentinel-2 L2A from Microsoft Planetary Computer.

For every region in regions.json it builds one July-August OSAVI composite per
year, flags olive-grove cells whose summer OSAVI fell sharply compared with the
previous summer, and keeps a per-cell history. Each scan is therefore compared
with all earlier seasons, and every flagged area is labelled:

    new         flagged for the first time in the latest season
    continuing  flagged in the latest season and in an earlier one
    earlier     flagged in an earlier season, did not recover, not flagged now

Usage
    python xylella_scanner.py --region puglia-ostuni
    python xylella_scanner.py --all
    python xylella_scanner.py --region puglia-ostuni --today 2026-09-15   # testing

Outputs per region, in data/<region-id>/
    zones.geojson   polygons with status, first season, area, shape
    history.json    flagged area per season
    status.json     scan date, seasons, scene counts, mask and parameter info
    state.npz       per-cell history (needed for the next run; keep it in git)

What this can and cannot see
    At 10 m a pixel holds roughly one olive crown plus soil and grass, so only
    advanced decline is visible, not early or symptomless infection
    (Zarco-Tejada et al. 2018; Hornero et al. 2020). A flagged area is a lead
    for a field check, never a diagnosis: drought, fire, abandonment and
    removal of trees for other reasons look the same from orbit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
REGIONS_FILE = ROOT / "regions.json"
DATA_DIR = ROOT / "data"
MASK_DIR = ROOT / "masks"

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# --------------------------------------------------------------------------
# Detection parameters. Changing any of them makes the next run rebuild the
# whole history, so all seasons are judged by the same rules.
# --------------------------------------------------------------------------
PIXEL_M = 10
FIRST_SEASON = 2019          # first season judged; 2017-2018 serve as baselines
SEASON = ("07-01", "08-31")  # dry-season window used by Hornero 2020 and Blonda 2023
SCENE_CLOUD_MAX = 30         # scene-level filter; pixels are masked with SCL anyway
MIN_DATES = 3                # valid observations per pixel per season
VALID_SCL = (4, 5)           # vegetation, bare soil
REFLECTANCE_OFFSET = 1000    # added to L2A digital numbers from processing baseline 04.00

# Decline rule (unchanged from the previous calibration):
# symptomatic olive crowns at 10 m sit roughly 45-55 % below healthy ones (Scala et
# al. 2024), so a 30 % drop plus at least 0.04 OSAVI marks clear decline.
BASELINE_OSAVI_MIN = 0.12    # healthy olive pixels sit near 0.12-0.16 OSAVI in summer
MIN_REL_DROP = 0.30
MIN_ABS_DROP = 0.04

# New rules, added after comparing scan output with the official picture.
# 1. Summer-green fields are not olive groves. Irrigated vegetables and fodder
#    peak far above an olive crown at 10 m, and their year-to-year swings were a
#    likely source of the patches along the coastal road.
BASELINE_OSAVI_MAX = 0.50
# 2. Olive trees are evergreen perennials with steady summer greenness. A cell
#    whose baseline summer was much greener than the summer before is a rotating
#    crop or a one-off green-up, not a grove that later declined.
MAX_BASELINE_RISE = 0.25     # baseline may be at most 25 % above the summer before it
# 3. Xylella decline is progressive: infected olives do not green up again.
#    Cells that win back at least half of the drop the next summer were pruned,
#    drought-stressed or cut for hay, and are removed from the record.
RECOVERY_SHARE = 0.50
# 4. Built-up land and its edges caused the large patches at Rosa Marina and
#    Villanova. Only tree, shrub, grass and cropland cells (ESA WorldCover 2021)
#    are judged, and two pixels around built-up land are excluded.
KEEP_WORLDCOVER = (10, 20, 30, 40)
BUILTUP_CLASS = 50
BUILTUP_BUFFER_PX = 2
# 5. Patch size. An infected-zone delimitation starts at 50 m around a tree; three
#    pixels (300 m2) is the smallest patch kept.
MIN_AREA_M2 = 300
# 6. Shape. Whole groves die and get uprooted in the infected zone, so rectangular
#    patches are no longer dropped. Large compact ones are labelled
#    'parcel-shaped' so the app can show them as possible tree removal.
PARCEL_EXTENT = 0.75
PARCEL_MIN_M2 = 2000

PARAMS = {
    "first_season": FIRST_SEASON, "season": SEASON, "scene_cloud_max": SCENE_CLOUD_MAX,
    "min_dates": MIN_DATES, "valid_scl": VALID_SCL, "reflectance_offset": REFLECTANCE_OFFSET,
    "baseline_osavi_min": BASELINE_OSAVI_MIN, "baseline_osavi_max": BASELINE_OSAVI_MAX,
    "min_rel_drop": MIN_REL_DROP, "min_abs_drop": MIN_ABS_DROP,
    "max_baseline_rise": MAX_BASELINE_RISE, "recovery_share": RECOVERY_SHARE,
    "keep_worldcover": KEEP_WORLDCOVER, "builtup_buffer_px": BUILTUP_BUFFER_PX,
    "min_area_m2": MIN_AREA_M2, "parcel_extent": PARCEL_EXTENT, "parcel_min_m2": PARCEL_MIN_M2,
}

BIT_BASE_YEAR = 2010         # bit k of the history masks = season 2010 + k
STATUS_NAMES = {1: "new", 2: "continuing", 3: "earlier"}
PX_HA = PIXEL_M * PIXEL_M / 10_000


# --------------------------------------------------------------------------
# Pure array logic (no network, unit-tested)
# --------------------------------------------------------------------------
def latest_season(today: dt.date) -> tuple[int, bool]:
    """Most recent season with usable imagery and whether that season is over."""
    if today >= dt.date(today.year, 7, 15):
        return today.year, today >= dt.date(today.year, 9, 1)
    return today.year - 1, True


def season_bit(season: int) -> np.uint32:
    return np.uint32(1 << (season - BIT_BASE_YEAR))


def decline_mask(cur, base, prev=None):
    """Cells whose summer OSAVI fell clearly from the baseline summer `base`."""
    with np.errstate(invalid="ignore", divide="ignore"):
        drop = base - cur
        ok = np.isfinite(cur) & np.isfinite(base)
        ok &= (base >= BASELINE_OSAVI_MIN) & (base <= BASELINE_OSAVI_MAX)
        ok &= drop >= MIN_ABS_DROP
        ok &= drop >= MIN_REL_DROP * base
        if prev is not None:
            greened = np.isfinite(prev) & (base > prev * (1 + MAX_BASELINE_RISE))
            ok &= ~greened
    return ok


def recovered_mask(base, cur, nxt):
    """Cells that won back at least RECOVERY_SHARE of their drop one summer later."""
    with np.errstate(invalid="ignore"):
        return np.isfinite(nxt) & np.isfinite(cur) & ((nxt - cur) >= RECOVERY_SHARE * (base - cur))


def remove_small(mask, min_px: int):
    """Drop 8-connected patches smaller than min_px cells."""
    from scipy import ndimage
    lab, n = ndimage.label(mask, structure=np.ones((3, 3), bool))
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    keep = sizes >= min_px
    keep[0] = False
    return keep[lab]


def update_history(comps: dict, seasons, landmask, flagged, recovered) -> None:
    """Recompute the flag bits for `seasons` and the recovery bits wherever the
    following summer is available. Arrays are updated in place."""
    min_px = max(1, int(np.ceil(MIN_AREA_M2 / PIXEL_M ** 2)))
    for s in seasons:
        b = season_bit(s)
        flagged &= ~b
        cur, base = comps.get(s), comps.get(s - 1)
        if cur is None or base is None:
            continue
        m = decline_mask(cur, base, comps.get(s - 2)) & landmask
        flagged[remove_small(m, min_px)] |= b
    for s in sorted(comps):
        if any(comps.get(y) is None for y in (s - 1, s, s + 1)):
            continue
        b = season_bit(s)
        recovered &= ~b
        rec = recovered_mask(comps[s - 1], comps[s], comps[s + 1]) & ((flagged & b) != 0)
        recovered[rec] |= b


def classify(flagged, recovered, latest: int):
    """Status raster (0 none, 1 new, 2 continuing, 3 earlier) and first flagged season."""
    eff = flagged & ~recovered
    lb = season_bit(latest)
    in_latest = (eff & lb) != 0
    before = (eff & np.uint32(lb - 1)) != 0
    status = np.zeros(eff.shape, np.uint8)
    status[in_latest & ~before] = 1
    status[in_latest & before] = 2
    status[~in_latest & before] = 3
    first = np.zeros(eff.shape, np.uint16)
    for s in range(latest, BIT_BASE_YEAR - 1, -1):
        first[(eff & season_bit(s)) != 0] = s
    return status, first


def season_stats(flagged, recovered, seasons) -> dict:
    eff = flagged & ~recovered
    out = {}
    for s in seasons:
        b = season_bit(s)
        now = (eff & b) != 0
        seen_before = (eff & np.uint32(b - 1)) != 0
        out[s] = {
            "new_ha": round(float((now & ~seen_before).sum()) * PX_HA, 2),
            "again_ha": round(float((now & seen_before).sum()) * PX_HA, 2),
            "recovered_ha": round(float(((flagged & recovered & b) != 0).sum()) * PX_HA, 2),
        }
    return out


def ring_area(ring) -> float:
    xs = np.asarray([p[0] for p in ring])
    ys = np.asarray([p[1] for p in ring])
    return 0.5 * abs(float(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))))


def polygon_area(geom) -> float:
    rings = geom["coordinates"]
    return ring_area(rings[0]) - sum(ring_area(r) for r in rings[1:])


def round_coords(obj, nd=6):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(v), nd) for v in obj]
        return [round_coords(o, nd) for o in obj]
    return obj


def params_hash(region: dict) -> str:
    blob = json.dumps({"params": PARAMS, "bbox": region["bbox"]}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Geospatial I/O (needs the conda environment from the workflow)
# --------------------------------------------------------------------------
def region_geobox(bbox):
    """10 m grid in the region's UTM zone, identical for every season."""
    from odc.geo.geobox import GeoBox
    from odc.geo.geom import box
    lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    epsg = (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1
    bb = box(*bbox, "EPSG:4326").to_crs(f"EPSG:{epsg}").boundingbox
    return GeoBox.from_bbox(bb, resolution=PIXEL_M)


def open_catalog():
    import planetary_computer
    import pystac_client
    return pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)


def _processing_baseline(item) -> float:
    v = item.properties.get("s2:processing_baseline")
    try:
        return float(v)
    except (TypeError, ValueError):
        when = item.datetime.date() if item.datetime else dt.date(2000, 1, 1)
        return 4.0 if when >= dt.date(2022, 1, 25) else 2.0


def load_season(catalog, gbox, bbox, year: int):
    """Median July-August OSAVI for one year (NaN where too few clear dates)."""
    import odc.stac
    import xarray as xr

    items = catalog.search(
        collections=["sentinel-2-l2a"], bbox=bbox,
        datetime=f"{year}-{SEASON[0]}/{year}-{SEASON[1]}",
        query={"eo:cloud_cover": {"lt": SCENE_CLOUD_MAX}},
    ).item_collection()
    if len(items) == 0:
        return None, 0

    groups: dict[int, list] = {}
    for it in items:
        off = REFLECTANCE_OFFSET if _processing_baseline(it) >= 4.0 else 0
        groups.setdefault(off, []).append(it)

    parts = []
    for off, its in groups.items():
        ds = odc.stac.load(its, bands=["B04", "B08", "SCL"], geobox=gbox,
                           groupby="solar_day", chunks={"x": 2048, "y": 2048})
        red = ((ds["B04"].astype("float32") - off) / 10000).clip(min=0)
        nir = ((ds["B08"].astype("float32") - off) / 10000).clip(min=0)
        valid = ds["SCL"].isin(list(VALID_SCL)) & (ds["B04"] > 0) & (ds["B08"] > 0)
        parts.append((1.16 * (nir - red) / (nir + red + 0.16)).where(valid))
    osavi = parts[0] if len(parts) == 1 else xr.concat(parts, dim="time")
    n_valid = osavi.notnull().sum("time")
    med = osavi.median("time", skipna=True).where(n_valid >= MIN_DATES)
    return med.compute().values.astype("float32"), int(osavi.sizes["time"])


def build_landmask(catalog, gbox, bbox, region_id: str):
    """Cells allowed to be judged, plus a description for status.json."""
    from scipy import ndimage
    shape = tuple(gbox.shape)
    mask = np.ones(shape, bool)
    info = {"worldcover": "not used", "olive_mask": None}
    try:
        import odc.stac
        items = catalog.search(collections=["esa-worldcover"], bbox=bbox,
                               datetime="2021-01-01/2021-12-31").item_collection()
        if len(items):
            wc = odc.stac.load(items, bands=["map"], geobox=gbox)["map"].max("time").values
            built = ndimage.binary_dilation(wc == BUILTUP_CLASS, iterations=BUILTUP_BUFFER_PX)
            mask &= np.isin(wc, KEEP_WORLDCOVER) & ~built
            info["worldcover"] = "ESA WorldCover 2021"
            info["worldcover_share_kept"] = round(float(mask.mean()), 3)
        else:
            info["worldcover"] = "no WorldCover tile found"
    except Exception as exc:  # the scan still runs, just less filtered
        info["worldcover"] = f"failed: {exc.__class__.__name__}"

    olive_file = MASK_DIR / f"{region_id}.geojson"
    if olive_file.exists():
        from rasterio import features
        from rasterio.warp import transform_geom
        fc = json.loads(olive_file.read_text())
        crs = str(gbox.crs)
        shapes = [(transform_geom("EPSG:4326", crs, f["geometry"]), 1)
                  for f in fc.get("features", []) if f.get("geometry")]
        if shapes:
            inside = features.rasterize(shapes, out_shape=shape, transform=gbox.transform,
                                        fill=0, dtype="uint8") == 1
            mask &= inside
            info["olive_mask"] = olive_file.name
    return mask, info


def vectorize(status, first, latest: int, transform, crs: str) -> list:
    """Polygons in EPSG:4326 with status, first season, area and shape."""
    from rasterio import features
    from rasterio.warp import transform_geom
    from scipy import ndimage

    any_ = status > 0
    comp, ncomp = ndimage.label(any_, structure=np.ones((3, 3), bool))
    if ncomp == 0:
        return []
    comp_px = np.bincount(comp.ravel(), minlength=ncomp + 1)
    slices = ndimage.find_objects(comp)
    key = np.where(any_, comp.astype(np.int64) * 4 + status, 0).astype(np.int32)
    keys = np.unique(key[key > 0])
    first_min = ndimage.minimum(np.where(any_, first, 9999), labels=key, index=keys)
    first_by_key = {int(k): int(v) for k, v in zip(keys, np.atleast_1d(first_min))}

    feats = []
    for geom, val in features.shapes(key, mask=key > 0, transform=transform):
        k = int(val)
        cid, code = divmod(k, 4)
        rows, cols = slices[cid - 1]
        bbox_px = (rows.stop - rows.start) * (cols.stop - cols.start)
        patch_m2 = int(comp_px[cid]) * PIXEL_M ** 2
        extent = comp_px[cid] / bbox_px
        shape = "parcel-shaped" if (patch_m2 >= PARCEL_MIN_M2 and extent >= PARCEL_EXTENT) else "irregular"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": round_coords(transform_geom(crs, "EPSG:4326", geom)["coordinates"])},
            "properties": {
                "status": STATUS_NAMES[code],
                "first_season": first_by_key[k],
                "latest_season": latest,
                "area_m2": int(round(polygon_area(geom))),
                "patch_area_m2": patch_m2,
                "extent": round(float(extent), 2),
                "shape": shape,
            },
        })
    return feats


# --------------------------------------------------------------------------
# State and outputs
# --------------------------------------------------------------------------
def load_state(path: Path):
    if not path.exists():
        return None
    with np.load(path) as z:
        meta = json.loads(str(z["meta"]))
        return {"flagged": z["flagged"].copy(), "recovered": z["recovered"].copy(), **meta}


def save_state(path: Path, flagged, recovered, meta: dict) -> None:
    np.savez_compressed(path, flagged=flagged, recovered=recovered, meta=np.array(json.dumps(meta)))


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def scan_region(region: dict, today: dt.date) -> dict:
    out = DATA_DIR / region["id"]
    out.mkdir(parents=True, exist_ok=True)
    latest, complete = latest_season(today)
    gbox = region_geobox(region["bbox"])
    shape = list(gbox.shape)
    phash = params_hash(region)

    state = load_state(out / "state.npz")
    reuse = bool(state) and state.get("params_hash") == phash and state.get("shape") == shape
    if state and not reuse:
        print("Parameters or area changed: rebuilding the full history.")

    if reuse and state.get("complete_through", 0) >= latest and (out / "status.json").exists():
        status = json.loads((out / "status.json").read_text())
        status["last_checked"] = today.isoformat()
        write_json(out / "status.json", status)
        print(f"{region['id']}: season {latest} already final, nothing new to scan.")
        return status

    if reuse:
        flagged, recovered = state["flagged"], state["recovered"]
        history = {int(k): v for k, v in state.get("history", {}).items()}
        evaluate = [s for s in (latest - 1, latest) if s >= FIRST_SEASON]
    else:
        flagged = np.zeros(shape, np.uint32)
        recovered = np.zeros(shape, np.uint32)
        history = {}
        evaluate = list(range(FIRST_SEASON, latest + 1))

    catalog = open_catalog()
    landmask, mask_info = build_landmask(catalog, gbox, region["bbox"], region["id"])

    comps, dates = {}, {}
    for year in range(min(evaluate) - 2, latest + 1):
        print(f"{region['id']}: loading summer {year} ...", flush=True)
        try:
            comps[year], dates[year] = load_season(catalog, gbox, region["bbox"], year)
        except Exception as exc:
            print(f"  failed ({exc.__class__.__name__}: {exc}); season skipped")
            comps[year], dates[year] = None, 0
        if comps[year] is None:
            print("  not enough clear imagery")

    update_history(comps, evaluate, landmask, flagged, recovered)

    all_seasons = list(range(FIRST_SEASON, latest + 1))
    stats = season_stats(flagged, recovered, all_seasons)
    for s in all_seasons:
        entry = history.get(s, {})
        if s in dates:
            entry["dates"] = dates[s]
            entry["judged"] = comps.get(s) is not None and comps.get(s - 1) is not None
        entry.update(stats[s])
        entry["complete"] = s < latest or complete
        history[s] = entry

    status_r, first = classify(flagged, recovered, latest)
    features = vectorize(status_r, first, latest, gbox.transform, str(gbox.crs))
    write_json(out / "zones.geojson", {"type": "FeatureCollection", "features": features})
    write_json(out / "history.json", [{"season": s, **history[s]} for s in all_seasons])

    base = comps.get(latest - 1)
    share = None
    if base is not None and landmask.any():
        b = base[landmask]
        share = round(float(((b >= BASELINE_OSAVI_MIN) & (b <= BASELINE_OSAVI_MAX)).mean()), 3)

    totals = {name: round(float((status_r == code).sum()) * PX_HA, 2) for code, name in STATUS_NAMES.items()}
    have_latest = comps.get(latest) is not None and comps.get(latest - 1) is not None
    status = {
        "region": region["id"],
        "name": region["name"],
        "scan_date": today.isoformat(),
        "last_checked": today.isoformat(),
        "latest_season": latest,
        "season_complete": complete,
        "season_window": f"{SEASON[0]} to {SEASON[1]}",
        "compared_with": latest - 1,
        "result": ("ok" if have_latest else "no usable imagery for the latest comparison"),
        "totals_ha": totals,
        "polygons": len(features),
        "baseline_share_in_range": share,
        "masks": mask_info,
        "parameters": PARAMS,
    }
    write_json(out / "status.json", status)

    meta = {"params_hash": phash, "shape": shape, "crs": str(gbox.crs),
            "transform": list(gbox.transform)[:6],
            "complete_through": latest if complete else latest - 1,
            "history": {str(k): v for k, v in history.items()}}
    save_state(out / "state.npz", flagged, recovered, meta)
    print(f"{region['id']}: {totals} (ha), {len(features)} polygons")
    return status


def load_regions() -> list:
    return json.loads(REGIONS_FILE.read_text())["regions"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--region", help="region id from regions.json")
    g.add_argument("--all", action="store_true", help="scan every enabled region")
    ap.add_argument("--today", help="pretend date (YYYY-MM-DD), for testing")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    regions = load_regions()
    if args.all:
        todo = [r for r in regions if r.get("enabled", True)]
    else:
        todo = [r for r in regions if r["id"] == args.region]
        if not todo:
            print(f"Unknown region {args.region!r}", file=sys.stderr)
            return 2
    for region in todo:
        scan_region(region, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
