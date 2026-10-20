import sys

import geopandas as gpd
import odc.geo.xr  # noqa: F401  (registers the .odc accessor)
import odc.stac
import planetary_computer
import pystac_client
import rasterio.features

# --- CONFIGURATION ---
# The real STAC API endpoint of Microsoft's Planetary Computer
# (NOT https://microsoft.com, which is just the consumer homepage).
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Coordinates: focus area in Puglia (lon/lat)
BBOX = [17.55, 40.75, 17.65, 40.80]
TIME_BASELINE = "2023-07-01/2023-08-30"
TIME_CURRENT = "2024-07-01/2024-08-30"
OUTPUT_FILE = "xylella_risk_zones.geojson"

# Puglia at ~17.6 E lies in UTM zone 33N. Fixing the CRS guarantees that the
# baseline and current rasters sit on exactly the same grid, so they can be
# subtracted cell by cell.
WORK_CRS = "EPSG:32633"


def write_empty_output():
    """Write a valid, empty GeoJSON (more reliable than exporting an empty GeoDataFrame)."""
    with open(OUTPUT_FILE, "w") as f:
        f.write('{"type": "FeatureCollection", "features": []}')


def load_data(bbox, time_window):
    """Fetch Sentinel-2 data, mask clouds, return the median OSAVI (or None if no scenes)."""
    # The modifier signs asset URLs; without it, reading the actual pixels fails with 403 errors.
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
        return None

    print(f"Loaded {len(items)} matching frames for {time_window}...")

    data = odc.stac.load(
        items,
        bands=["B04", "B08", "SCL"],
        crs=WORK_CRS,
        resolution=10,
        bbox=bbox,
        groupby="solar_day",
        chunks={},
    )

    # Calculate OSAVI
    red = data["B04"].astype("float32") / 10000
    nir = data["B08"].astype("float32") / 10000
    osavi = (1 + 0.16) * (nir - red) / (nir + red + 0.16)

    # Mask clouds/shadows (keep vegetation=4, bare soil=5)
    valid_mask = (data["SCL"] == 4) | (data["SCL"] == 5)
    return osavi.where(valid_mask).median(dim="time", keep_attrs=True)


print("🛰️  Scanning Satellite Imagery...")
osavi_old = load_data(BBOX, TIME_BASELINE)
osavi_new = load_data(BBOX, TIME_CURRENT)

if osavi_old is None or osavi_new is None:
    write_empty_output()
    sys.exit(0)

# 1. DETECT DECLINE
delta = osavi_new - osavi_old
decline_mask = (delta < -0.15) & (osavi_old > 0.3)
decline_mask = decline_mask.fillna(False).astype("uint8")

# 2. VECTORIZE (raster -> polygons)
print("📐  Vectorizing infection zones...")
mask_values = decline_mask.values
results = (
    {"properties": {"delta_value": v}, "geometry": s}
    for s, v in rasterio.features.shapes(
        mask_values,
        mask=(mask_values == 1),
        transform=decline_mask.odc.geobox.transform,
    )
)

gdf = gpd.GeoDataFrame.from_features(list(results))
if gdf.empty:
    print("✅ No significant vegetation health decline found. Saving empty template.")
    write_empty_output()
    sys.exit(0)

gdf = gdf.set_crs(WORK_CRS)

# 3. SMART FILTERING (the "anti-pruning" logic)
print(f"🔍  Analyzing {len(gdf)} potential structural components...")
gdf["area_m2"] = gdf.area
gdf["extent"] = gdf.area / (gdf.geometry.envelope.area + 0.001)

# Keep irregular shapes, remove perfect rows/rectangles
risk_zones = gdf[(gdf["area_m2"] > 300) & (gdf["extent"] < 0.75)].copy()

if risk_zones.empty:
    print("✅ Post-filter processing confirmed zero disease patterns.")
    write_empty_output()
    sys.exit(0)

# 4. EXPORT
risk_zones = risk_zones.to_crs(epsg=4326)
risk_zones.to_file(OUTPUT_FILE, driver="GeoJSON")

print(f"🚨 SUCCESS: Found {len(risk_zones)} high-probability infection zones.")
print(f"💾 File updated and exported to: {OUTPUT_FILE}")
