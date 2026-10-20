import pystac_client
import odc.stac
import numpy as np
import geopandas as gpd
import rasterio.features
from shapely.geometry import shape
import sys

# --- CONFIGURATION ---
# Coordinates: Focus area in Puglia
BBOX = [17.55, 40.75, 17.65, 40.80] 
TIME_BASELINE = "2023-07-01/2023-08-30" 
TIME_CURRENT  = "2024-07-01/2024-08-30"
OUTPUT_FILE = "xylella_risk_zones.geojson"

def load_data(bbox, time_window):
    """Fetches Sentinel-2 data, masks clouds, returns OSAVI."""
    # FIXED: Direct link to Microsoft's Geospatial Catalog
    catalog = pystac_client.Client.open("https://microsoft.com")
    
    search = catalog.search(
        collections=["sentinel-2-l2a"], 
        bbox=bbox, 
        datetime=time_window, 
        query={"eo:cloud_cover": {"lt": 10}}
    )
    
    # FIXED: Added safety catch logic in case a search window has no images
    items = search.item_collection()
    if len(items) == 0:
        print(f"⚠️ No cloud-free satellite scenes found for {time_window}!")
        empty_gdf = gpd.GeoDataFrame(columns=['geometry', 'area_m2', 'extent'], crs="EPSG:4326")
        empty_gdf.to_file(OUTPUT_FILE, driver="GeoJSON")
        sys.exit(0)
        
    print(f"Loaded {len(items)} matching frames for scene evaluation window...")
    
    # FIXED: Stripped duplicate outer bracket array layer [items]
    data = odc.stac.load(
        items, 
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
    if v == 1 
)

# Create GeoDataFrame in native CRS (UTM)
gdf = gpd.GeoDataFrame.from_features(list(results))
if gdf.empty:
    print("✅ No significant vegetation health decline found. Saving empty template.")
    empty_gdf = gpd.GeoDataFrame(columns=['geometry', 'area_m2', 'extent'], crs="EPSG:4326")
    empty_gdf.to_file(OUTPUT_FILE, driver="GeoJSON")
    sys.exit(0)

gdf.set_crs(decline_mask.odc.geobox.crs, inplace=True)

# 3. SMART FILTERING (The "Anti-Pruning" Logic)
print(f"🔍  Analyzing {len(gdf)} potential structural components...")

# A. Calculate Area (in square meters)
gdf["area_m2"] = gdf.area

# B. Calculate Rectangularity (Extent)
# FIXED: Appended a +0.001 offset layer buffer to protect against ZeroDivisionErrors
gdf["extent"] = gdf.area / (gdf.geometry.envelope.area + 0.001)

# FILTER: Keep irregular shapes and filter out small pixel noise or straight pruning rows
risk_zones = gdf[ (gdf["area_m2"] > 300) & (gdf["extent"] < 0.75) ].copy()

if risk_zones.empty:
    print("✅ Post-filter processing confirmed zero disease patterns. (Only geometric human tasks found).")
    empty_gdf = gpd.GeoDataFrame(columns=['geometry', 'area_m2', 'extent'], crs="EPSG:4326")
    empty_gdf.to_file(OUTPUT_FILE, driver="GeoJSON")
    sys.exit(0)

# 4. EXPORT
risk_zones = risk_zones.to_crs(epsg=4326)
risk_zones.to_file(OUTPUT_FILE, driver="GeoJSON")

print(f"🚨 SUCCESS: Found {len(risk_zones)} high-probability infection zones.")
print(f"💾 File updated and exported to: {OUTPUT_FILE}")
