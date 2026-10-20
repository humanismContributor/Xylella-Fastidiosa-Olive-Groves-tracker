import streamlit as st
import folium
from streamlit_folium import st_folium
import geopandas as gpd

st.set_page_config(page_title="Xylella Watch", layout="wide")

st.title("🛰️ Xylella Fastidiosa Early Warning System")
st.markdown("**Automated Satellite Surveillance | Region: Puglia, Italy**")

# Load the data
try:
    gdf = gpd.read_file("xylella_risk_zones.geojson")
    data_exists = True
except Exception:
    st.warning("No infection data currently available. Run the scanner script.")
    data_exists = False

# Sidebar Stats
st.sidebar.header("Scan Report")
if data_exists:
    total_area = gdf['area_m2'].sum() / 10000 # convert to hectares
    st.sidebar.metric("Potential Affected Area", f"{total_area:.2f} ha")
    st.sidebar.metric("Detected Outbreaks", len(gdf))
    st.sidebar.info("Zones shown have been filtered to exclude mechanical pruning.")

# CREATE INTERACTIVE MAP
# Start centered on Puglia
m = folium.Map(location=[40.7, 17.6], zoom_start=12, tiles="CartoDB positron")

if data_exists:
    # Add the infection zones
    folium.GeoJson(
        gdf,
        name="High Risk Zones",
        style_function=lambda x: {
            "fillColor": "#ff0000", # Red
            "color": "#8b0000",     # Dark Red Border
            "weight": 2,
            "fillOpacity": 0.6,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["area_m2", "extent"],
            aliases=["Area (m²):", "Rectangularity:"],
            localize=True
        )
    ).add_to(m)

# Render Map in Streamlit
st_folium(m, width="100%", height=600)

st.caption("Data Source: Sentinel-2 (ESA) | Processing: Microsoft Planetary Computer")
