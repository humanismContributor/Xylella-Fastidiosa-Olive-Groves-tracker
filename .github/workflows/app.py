import streamlit as st
import glob
from PIL import Image

st.title("🇮🇹 Xylella Fastidiosa Tracker")
st.write("Automated weekly analysis of Sentinel-2 satellite imagery.")

# Sidebar for controls
st.sidebar.header("Filter Options")
available_maps = sorted(glob.glob("results/*.png"))
selected_date = st.sidebar.selectbox("Select Analysis Date", available_maps)

# Display the Map
if selected_date:
    image = Image.open(selected_date)
    st.image(image, caption=f"Analysis from {selected_date}", use_column_width=True)
    
st.info("ℹ️ Orange zones indicate high-probability new infections (extrapolated).")
