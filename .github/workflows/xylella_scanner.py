import pystac_client
import odc.stac
import numpy as np
import geopandas as gpd
import rasterio.features
from shapely.geometry import shape

# --- CONFIGURATION ---
# Coordinates: Focus area in Puglia
BBOX = [17.55, 40.75, 17.65, 40.80] 
TIME_BASELINE = "2023-07-01/2023-08-30" 
TIME_CURRENT  = "2024-07-01/2024-08-30"
OUTPUT_FILE = "xylella_risk_zones.geojson"

def load_data(bbox, time_window):
    """Fetches Sentinel-2 data, masks clouds, returns OSAVI."""
    catalog = pystac_client.Client.open("https://microsoft.com")
    search = catalog.search(
        collections=["sentinel-2-l2a"], bbox=bbox, datetime=time_window, 
        query={"eo:cloud_cover": {"lt": 10}}
    )
    # Load data with native projection (UTM) to ensure accurate area calc
    data = odc.stac.load(
        [search.item_collection()], 
        bands=["B04", "B08", "SCL"], 
        resolution=10, 
        bbox=bbox,
        chunks={} # Enable lazy loading
    )
    
    # Calculate OSAVI
    red = data["B04"].astype("float32") / 10000
    nir = data["B08"].astype("float32") / 10000
    osavi = (1 + 0.16) * (nir - red) / (nir + red + 0.16)
    
    # Mask Clouds/Shadows (Keep Veg=4, Soil=5)
    valid_mask = (data["SCL"] == 4) | (data["SCL"] == 5)
    return osavi.where(valid_mask).median(dim="time", keep_attrs=True)

print("🛰️  Scanning Satellite Imagery...")
osavi_old = load_data(BBOX, TIME_BASELINE)
osavi_new = load_data(BBOX, TIME_CURRENT)

# 1. DETECT DECLINE
# Threshold: Drop > 0.15 AND was previously healthy
delta = osavi_new - osavi_old
decline_mask = (delta < -0.15) & (osavi_old > 0.3)
decline_mask = decline_mask.fillna(False).astype("uint8")

# 2. VECTORIZE (Raster -> Polygons)
print("📐  Vectorizing infection zones...")
results = (
    {'properties': {'delta_value': v}, 'geometry': s}
    for i, (s, v) in enumerate(
        rasterio.features.shapes(decline_mask.values, transform=decline_mask.odc.geobox.transform)
    )
    if v == 1 # Keep only "True" (Decline) areas
)

# Create GeoDataFrame in native CRS (UTM)
gdf = gpd.GeoDataFrame.from_features(list(results))
if gdf.empty:
    print("✅ No significant decline found.")
    exit()

gdf.set_crs(decline_mask.odc.geobox.crs, inplace=True)

# 3. SMART FILTERING (The "Anti-Pruning" Logic)
print(f"🔍  Analyzing {len(gdf)} potential zones...")

# A. Calculate Area (in square meters)
gdf["area_m2"] = gdf.area

# B. Calculate Rectangularity (Extent)
# Extent = Area / Area of Bounding Box. 
# 1.0 = Perfect Rectangle (likely pruning). < 0.7 = Irregular (likely disease).
gdf["extent"] = gdf.area / gdf.geometry.envelope.area

# FILTER: 
# 1. Keep spots > 300m2 (ignore pixel noise)
# 2. Keep spots with Extent < 0.75 (ignore perfect rectangles)
risk_zones = gdf[ (gdf["area_m2"] > 300) & (gdf["extent"] < 0.75) ].copy()

# 4. EXPORT
# Reproject to Lat/Lon (EPSG:4326) for web mapping
risk_zones = risk_zones.to_crs(epsg=4326)
risk_zones.to_file(OUTPUT_FILE, driver="GeoJSON")

print(f"🚨  Found {len(risk_zones)} high-probability infection zones.")
print(f"💾  Saved to {OUTPUT_FILE}")
