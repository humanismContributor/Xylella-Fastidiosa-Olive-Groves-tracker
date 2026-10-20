"""Weekly Xylella early-warning scan for Puglia (Sentinel-2 via Planetary Computer).

Compares the median July-August OSAVI of the current summer with the previous
summer, flags irregular patches where vegetation health dropped, and writes:

  - xylella_risk_zones.geojson  decline zones in EPSG:4326 (empty if none)
  - scan_status.json            when/what was scanned, the outcome, diagnostics
"""
import json
from datetime import date, timedelta

import geopandas as gpd
import numpy as np
import odc.geo.xr  # noqa: F401  (registers the .odc accessor)
import odc.stac
import planetary_computer
import pystac_client
import rasterio.features
import xarray as xr
from shapely.geometry import shape

# --- CONFIGURATION ---
# The real STAC API endpoint of Microsoft's Planetary Computer
# (NOT https://microsoft.com, which is just the consumer homepage).
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Focus area in Puglia (lon/lat)
BBOX = [17.55, 40.75, 17.65, 40.80]

OUTPUT_FILE = "xylella_risk_zones.geojson"
STATUS_FILE = "scan_status.json"
EMPTY_GEOJSON = '{"type": "FeatureCollection", "features": []}'

# Puglia at ~17.6 E lies in UTM zone 33N. Fixing the CRS guarantees that the
# baseline and current rasters sit on exactly the same grid, so they can be
# subtracted cell by cell.
WORK_CRS = "EPSG:32633"

# --- Season ---
# July-August: the dry season, when grass between the trees contributes least to
# the 10 m signal (Blonda et al. 2023, Sci Rep; Hornero et al. 2020, RSE). The
# current summer is compared with the previous one, as in those studies.
SEASON_START = (7, 1)
SEASON_END = (8, 31)
MIN_SEASON_DAYS = 30  # a summer in progress needs this many days before it is used
MIN_DATES = 3         # minimum cloud-free dates per window (Blonda et al. used ~6/summer)

# --- Sentinel-2 L2A reflectance offset ---
# Since processing baseline 04.00 (25 Jan 2022) ESA adds +1000 to the DN values.
# It must be removed before computing indices, or OSAVI comes out roughly a
# quarter too low at olive-grove reflectances and can't be compared with
# published values (Planetary Computer's "baseline change" notebook).
S2_BASELINE_04_DATE = np.datetime64("2022-01-25")
S2_BOA_OFFSET = 1000

# --- Detection thresholds ---
# INFERRED from the literature, NOT validated for this area. No publication
# gives a Sentinel-2 cut-off for Xylella; what exists:
#   - Scala et al. 2024 (Plants): 10 m Sentinel-2 NDVI in Salento, healthy olive
#     ~0.15-0.20 vs symptomatic mostly <0.10 (a ~45-55% relative drop; OSAVI is
#     a little lower than NDVI at such sparse canopies, so ~0.05-0.08 absolute).
#   - Hornero et al. 2020 (RSE): year-on-year OSAVI/ARVI change tracks disease
#     incidence (r2 ~0.75); early, asymptomatic infection is not detectable.
#   - Zarco-Tejada et al. 2018 (Nature Plants): broadband indices such as NDVI
#     only detect advanced disease.
#   - Blonda et al. 2023 (Sci Rep): pruning, heat waves and drought cause
#     region-wide index drops (e.g. 2020), which is why a relative threshold
#     with a noise floor is used instead of a single absolute change.
MIN_BASELINE_OSAVI = 0.12   # above bare soil; sparse olive pixels sit near 0.12-0.16
MIN_RELATIVE_DROP = 0.30    # at least 30% below last summer (symptomatic contrast ~45-55%)
MIN_ABSOLUTE_DROP = 0.04    # and at least this much OSAVI, to ignore noise at low values
MIN_AREA_M2 = 300           # design choice, no published basis
MAX_EXTENT = 0.75           # design choice, no published basis (regular rectangles score ~1)


def scan_windows(today):
    """Return (baseline_window, current_window) as 'start/end' ISO date strings.

    Uses the current July-August once at least MIN_SEASON_DAYS of it have passed,
    this year's complete summer after August, and last year's summer before July.
    The baseline is the same dates one year earlier.
    """
    start_this = date(today.year, *SEASON_START)
    end_this = date(today.year, *SEASON_END)

    if start_this + timedelta(days=MIN_SEASON_DAYS) <= today <= end_this:
        start, end = start_this, today  # summer in progress
    elif today > end_this:
        start, end = start_this, end_this  # this year's completed summer
    else:
        start = date(today.year - 1, *SEASON_START)  # last year's completed summer
        end = date(today.year - 1, *SEASON_END)

    baseline = f"{start.replace(year=start.year - 1)}/{end.replace(year=end.year - 1)}"
    return baseline, f"{start}/{end}"


def to_reflectance(band):
    """Sentinel-2 DN -> surface reflectance, undoing the +1000 offset where it applies."""
    dn = band.astype("float32")
    without_offset = (dn - S2_BOA_OFFSET).clip(min=0)
    return xr.where(band["time"] >= S2_BASELINE_04_DATE, without_offset, dn) / 10000


def load_osavi(bbox, time_window):
    """Fetch Sentinel-2 data, mask clouds, return (median OSAVI, number of dates).

    Returns (None, n) when there are fewer than MIN_DATES cloud-free dates.
    """
    # The modifier signs asset URLs; without it, reading pixels fails with 403 errors.
    catalog = pystac_client.Client.open(
        STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )

    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime=time_window,
        query={"eo:cloud_cover": {"lt": 10}},
    )

    items = search.item_collection()
    if len(items) == 0:
        print(f"⚠️ No cloud-free satellite scenes found for {time_window}!")
        return None, 0

    data = odc.stac.load(
        items,
        bands=["B04", "B08", "SCL"],
        crs=WORK_CRS,
        resolution=10,
        bbox=bbox,
        groupby="solar_day",
        chunks={},
    )

    n_dates = int(data.sizes["time"])
    if n_dates < MIN_DATES:
        print(f"⚠️ Only {n_dates} cloud-free dates for {time_window} (need {MIN_DATES}).")
        return None, n_dates

    print(f"Using {n_dates} cloud-free dates for {time_window}...")

    # Calculate OSAVI
    red = to_reflectance(data["B04"])
    nir = to_reflectance(data["B08"])
    osavi = (1 + 0.16) * (nir - red) / (nir + red + 0.16)

    # Mask clouds/shadows (keep vegetation=4, bare soil=5)
    valid_mask = (data["SCL"] == 4) | (data["SCL"] == 5)
    median = osavi.where(valid_mask).median(dim="time", keep_attrs=True)
    return median.compute(), n_dates


def baseline_summary(osavi_old):
    """Distribution of baseline OSAVI, so the thresholds can be checked against real data."""
    values = osavi_old.values
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {}
    p10, p50, p90 = np.percentile(values, [10, 50, 90])
    return {
        "baseline_osavi_p10": round(float(p10), 3),
        "baseline_osavi_p50": round(float(p50), 3),
        "baseline_osavi_p90": round(float(p90), 3),
        "baseline_share_above_min": round(float((values > MIN_BASELINE_OSAVI).mean()), 3),
    }


def detect_decline(osavi_old, osavi_new):
    """Binary (uint8) mask of cells that were vegetated and clearly declined."""
    drop = osavi_old - osavi_new
    decline = (
        (osavi_old > MIN_BASELINE_OSAVI)
        & (drop >= MIN_ABSOLUTE_DROP)
        & (osavi_new <= osavi_old * (1 - MIN_RELATIVE_DROP))
    )
    return decline.astype("uint8")  # NaN comparisons are already False


def vectorize(decline_mask):
    """Turn the raster mask into polygons (in the raster's projected CRS)."""
    values = decline_mask.values
    geoms = [
        shape(geom)
        for geom, _ in rasterio.features.shapes(
            values,
            mask=(values == 1),
            transform=decline_mask.odc.geobox.transform,
        )
    ]
    return gpd.GeoDataFrame(geometry=geoms, crs=WORK_CRS)


def filter_zones(gdf):
    """Keep irregular patches of meaningful size; drop regular rectangles."""
    gdf = gdf.copy()
    gdf["area_m2"] = gdf.area.round(1)
    gdf["extent"] = (gdf.area / (gdf.geometry.envelope.area + 0.001)).round(3)
    keep = (gdf["area_m2"] > MIN_AREA_M2) & (gdf["extent"] < MAX_EXTENT)
    return gdf[keep].copy()


def write_outputs(zones, status):
    """Write the GeoJSON (empty if no zones) and the status file."""
    if zones is None or zones.empty:
        with open(OUTPUT_FILE, "w") as f:
            f.write(EMPTY_GEOJSON)
        count = 0
    else:
        zones.to_crs(epsg=4326).to_file(OUTPUT_FILE, driver="GeoJSON")
        count = len(zones)

    status["zones_found"] = count
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def main():
    today = date.today()
    baseline_window, current_window = scan_windows(today)
    status = {
        "scan_date": today.isoformat(),
        "baseline_window": baseline_window,
        "current_window": current_window,
    }

    print("🛰️  Scanning satellite imagery...")
    osavi_old, dates_old = load_osavi(BBOX, baseline_window)
    osavi_new, dates_new = load_osavi(BBOX, current_window)
    status["dates_baseline"] = dates_old
    status["dates_current"] = dates_new

    if osavi_old is None or osavi_new is None:
        status["result"] = "no_imagery"
        write_outputs(None, status)
        print("Not enough usable imagery this run; wrote empty results and status.")
        return

    status.update(baseline_summary(osavi_old))

    print("📉  Detecting vegetation decline...")
    decline_mask = detect_decline(osavi_old, osavi_new)
    status["decline_cells"] = int(decline_mask.sum())

    print("📐  Vectorizing decline areas...")
    candidates = vectorize(decline_mask)

    print(f"🔍  Analyzing {len(candidates)} potential structural components...")
    zones = filter_zones(candidates)

    status["result"] = "zones_found" if not zones.empty else "no_decline"
    write_outputs(zones, status)

    if zones.empty:
        print("✅ No significant decline patterns found.")
    else:
        print(f"🚨 Found {len(zones)} vegetation-decline zones.")
    print(f"💾 Saved {OUTPUT_FILE} and {STATUS_FILE}")


if __name__ == "__main__":
    main()
