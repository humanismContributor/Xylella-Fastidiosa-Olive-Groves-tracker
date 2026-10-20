import json
from pathlib import Path

import folium
import geopandas as gpd
import streamlit as st
from streamlit_folium import st_folium

GEOJSON_FILE = "xylella_risk_zones.geojson"
STATUS_FILE = "scan_status.json"

st.set_page_config(page_title="Xylella Watch", layout="wide")

st.title("🛰️ Xylella Fastidiosa Early Warning System")
st.markdown("**Automated Satellite Surveillance | Region: Puglia, Italy**")

# Load metadata about the latest scan (written by the scanner)
status = {}
status_path = Path(STATUS_FILE)
if status_path.exists():
    try:
        status = json.loads(status_path.read_text())
    except Exception:
        status = {}

# Load the decline zones
gdf = None
try:
    gdf = gpd.read_file(GEOJSON_FILE)
except Exception:
    st.warning("No scan data available yet. Run the scanner workflow.")

# An empty scan result is a valid outcome, not an error
data_exists = (
    gdf is not None
    and not gdf.empty
    and {"area_m2", "extent"}.issubset(gdf.columns)
)

result = status.get("result")
if result == "no_imagery":
    st.warning(
        "The latest scan could not run: not enough cloud-free satellite images "
        "in the comparison windows. Results will update on the next scan."
    )
elif gdf is not None and not data_exists:
    st.success("The latest scan found no significant vegetation decline.")

# Sidebar Stats
st.sidebar.header("Scan Report")
if status.get("scan_date"):
    st.sidebar.caption(f"Last scan: {status['scan_date']}")
    if status.get("current_window"):
        st.sidebar.caption(f"Current window: {status['current_window']}")
    if status.get("baseline_window"):
        st.sidebar.caption(f"Compared with: {status['baseline_window']}")

if data_exists:
    total_area = gdf["area_m2"].sum() / 10000  # convert to hectares
    st.sidebar.metric("Potential Affected Area", f"{total_area:.2f} ha")
    st.sidebar.metric("Detected Decline Zones", len(gdf))
    st.sidebar.info(
        "Zones favor irregular patches over regular rectangles, "
        "which helps filter out mechanical pruning and harvest."
    )

st.sidebar.warning(
    "Satellite-detected vegetation decline only, not a confirmed Xylella "
    "diagnosis. Drought, fire and abandoned fields can look similar. "
    "Verify in the field."
)

# CREATE INTERACTIVE MAP
# Centered on the scanned area (lon 17.55-17.65, lat 40.75-40.80)
m = folium.Map(location=[40.775, 17.6], zoom_start=13, tiles="OpenStreetMap")

if data_exists:
    # Add the decline zones
    folium.GeoJson(
        gdf,
        name="Vegetation Decline Zones",
        style_function=lambda x: {
            "fillColor": "#ff0000",  # Red
            "color": "#8b0000",      # Dark Red Border
            "weight": 2,
            "fillOpacity": 0.6,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["area_m2", "extent"],
            aliases=["Area (m²):", "Rectangularity:"],
            localize=True,
        ),
    ).add_to(m)

# Render Map in Streamlit
st_folium(m, width="100%", height=600)

st.caption("Data Source: Sentinel-2 (ESA) | Processing: Microsoft Planetary Computer")
