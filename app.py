"""Xylella Watch: satellite decline scans plus official Xylella records."""
import datetime as dt
import html
import json
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

STATUS = {
    "new": {"label": "New this season", "fill": "#F59E0B", "line": "#92400E", "order": 3},
    "continuing": {"label": "Declining again (seen before)", "fill": "#DC2626", "line": "#7F1D1D", "order": 2},
    "earlier": {"label": "Flagged in an earlier season", "fill": "#6B7280", "line": "#1F2937", "order": 1},
}
RECORD_NEW = "#F59E0B"
RECORD_OLD = "#991B1B"
PROJECTION = "#7C3AED"
ROLE_TEXT = {
    "established": "Inside the official infected zone: decline here is expected, and flagged areas mostly show how fast groves are still being lost.",
    "front": "At the advancing edge, where official sampling continues. Flagged areas can be checked against the Region's published results.",
    "outbreak": "Around a recent official outbreak. A flagged area is a lead for inspection, not a confirmed case.",
    "experimental": "Experimental: the thresholds were derived for olive groves, not for this crop.",
}
# Natural spread rates used for the projected envelope
SPREAD = {
    "pauca": (10.0, "about 10 km a year (95 % range 7.5–12.5), estimated from Puglia's monitoring data (Kottelenberg et al. 2021)"),
    "other": (1.9, "about 1.9 km a year, a southern-France estimate quoted by Kottelenberg et al. 2021 (rough)"),
}

st.set_page_config(page_title="Xylella Watch", page_icon="🫒", layout="wide")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600)
def read_json(path: str):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def regions():
    cfg = read_json(str(ROOT / "regions.json")) or {"regions": []}
    return [r for r in cfg["regions"] if r.get("enabled", True)]


def records():
    return (read_json(str(ROOT / "official_outbreaks.json")) or {"records": []})["records"]


def region_data(rid: str):
    folder = DATA / rid
    return (read_json(str(folder / "status.json")),
            read_json(str(folder / "zones.geojson")),
            read_json(str(folder / "history.json")))


# ---------------------------------------------------------------------------
# Map helpers
# ---------------------------------------------------------------------------
def legend(items):
    """items: list of (css for the swatch, text)"""
    spans = "".join(
        f'<span style="display:inline-flex;align-items:center;margin:0 18px 6px 0">'
        f'<span style="width:14px;height:14px;margin-right:6px;{css}"></span>{html.escape(text)}</span>'
        for css, text in items)
    st.markdown(f'<div style="font-size:0.9rem">{spans}</div>', unsafe_allow_html=True)


def record_popup(r):
    return folium.Popup(
        f"<b>{html.escape(r['place'])}</b> ({html.escape(r['country'])})<br>"
        f"{r['year']}: {html.escape(r['kind'])}<br>"
        f"Subspecies: {html.escape(r['subspecies'])}; host: {html.escape(r['host'])}<br>"
        f"<a href='{html.escape(r['source_url'])}' target='_blank'>{html.escape(r['source_title'])}</a>",
        max_width=320)


def add_records(m, recs, upto, highlight_year):
    for r in sorted(recs, key=lambda r: r["year"]):
        if r["year"] > upto:
            continue
        new = r["year"] == highlight_year
        folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=9 if new else 6,
            color="#111827" if new else RECORD_OLD,
            weight=2 if new else 1,
            fill=True, fill_color=RECORD_NEW if new else RECORD_OLD, fill_opacity=0.95 if new else 0.7,
            tooltip=f"{r['place']}: {r['year']} ({r['subspecies']})",
            popup=record_popup(r),
        ).add_to(m)


def add_projection(m, recs, upto, years_ahead=3):
    layer = folium.FeatureGroup(name="Projected natural spread", show=True)
    for r in recs:
        if not r.get("front") or not (upto - 2 <= r["year"] <= upto):
            continue
        km, _ = SPREAD["pauca"] if r["subspecies"] == "pauca" else SPREAD["other"]
        for k in range(years_ahead, 0, -1):
            folium.Circle(
                location=[r["lat"], r["lon"]], radius=km * k * 1000,
                color=PROJECTION, weight=1, dash_array="6 6",
                fill=True, fill_color=PROJECTION, fill_opacity=0.05,
                tooltip=f"{r['place']}: reach after {k} year(s) at ~{km:g} km/yr",
            ).add_to(layer)
    layer.add_to(m)


def add_zones(m, zones):
    feats = (zones or {}).get("features", [])
    for name, sty in sorted(STATUS.items(), key=lambda kv: kv[1]["order"]):
        subset = [f for f in feats if f["properties"].get("status") == name]
        if not subset:
            continue
        for f in subset:
            p = f["properties"]
            p["label"] = sty["label"]
            p["area_ha"] = round(p.get("area_m2", 0) / 10000, 2)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": subset},
            name=sty["label"],
            style_function=lambda f, s=sty: {
                "fillColor": s["fill"], "color": s["line"], "weight": 1.5, "fillOpacity": 0.65,
                "dashArray": "4 3" if f["properties"].get("shape") == "parcel-shaped" else None,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["label", "first_season", "area_ha", "shape"],
                aliases=["Status", "First flagged", "Area (ha)", "Shape"]),
        ).add_to(m)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def overview(regs, recs):
    st.subheader("Official records around the Mediterranean")
    years = sorted({r["year"] for r in recs}) or [dt.date.today().year]
    upto = st.sidebar.slider("Show official records up to", years[0], years[-1], years[-1])
    show_proj = st.sidebar.checkbox("Projected natural spread (3 years)", value=True)
    show_regions = st.sidebar.checkbox("Satellite scan areas", value=True)

    shown = [r for r in recs if r["year"] <= upto]
    c1, c2, c3 = st.columns(3)
    c1.metric("Records shown", len(shown))
    c2.metric(f"New in {upto}", sum(r["year"] == upto for r in recs))
    c3.metric("Countries", len({r["country"] for r in shown}))

    legend([(f"background:{RECORD_NEW};border:2px solid #111827;border-radius:50%", f"Reported in {upto}"),
            (f"background:{RECORD_OLD};border-radius:50%", "Reported earlier"),
            (f"border:1px dashed {PROJECTION};background:rgba(124,58,237,.12);border-radius:50%",
             "Reach after 1, 2 and 3 years of natural spread"),
            ("border:2px solid #2563EB", "Satellite scan area")])

    m = folium.Map(location=[40.0, 9.0], zoom_start=5, tiles="OpenStreetMap")
    if show_proj:
        add_projection(m, recs, upto)
    add_records(m, recs, upto, upto)
    if show_regions:
        for r in regs:
            s, _, _ = region_data(r["id"])
            w, so, e, n = r["bbox"]
            tot = (s or {}).get("totals_ha", {})
            summary = (f"Season {s['latest_season']}: {tot.get('new', 0)} ha new, "
                       f"{tot.get('continuing', 0)} ha again" if s else "not scanned yet")
            folium.Rectangle([[so, w], [n, e]], color="#2563EB", weight=2, fill=False,
                             tooltip=f"{r['name']}: {summary}").add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width="100%", height=600, returned_objects=[])

    st.caption(
        "Slide the year back to watch the spread unfold. The purple rings show how far Xylella could travel by "
        f"natural spread alone: for subsp. pauca {SPREAD['pauca'][1]}; for other subspecies {SPREAD['other'][1]}. "
        "Rings are drawn only around records at the edge of known spread from the last three years. They are not a "
        "forecast: insects carried by vehicles and moved plants cause long jumps, as with the Gargano (2025) and "
        "Calabria (2026) outbreaks, and no ring would have predicted those.")

    with st.expander(f"Records from {upto}"):
        rows = [{"Place": r["place"], "Country": r["country"], "Subspecies": r["subspecies"],
                 "Host": r["host"], "What happened": r["kind"], "Source": r["source_url"]}
                for r in recs if r["year"] == upto]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                         column_config={"Source": st.column_config.LinkColumn("Source")})
        else:
            st.write("No records for this year.")


def region_view(r, recs):
    st.subheader(r["name"])
    st.info(f"**Official status.** {r['official_status']}\n\n{ROLE_TEXT.get(r.get('role'), '')}")
    status, zones, history = region_data(r["id"])
    if not status:
        st.warning("This area hasn't been scanned yet. Start the 'Weekly Xylella Scan' workflow on GitHub.")
        return

    tot = status.get("totals_ha", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("New this season", f"{tot.get('new', 0):.2f} ha")
    c2.metric("Declining again", f"{tot.get('continuing', 0):.2f} ha")
    c3.metric("Earlier seasons", f"{tot.get('earlier', 0):.2f} ha")
    season = status["latest_season"]
    c4.metric("Comparison", f"{season} vs {status['compared_with']}",
              help="July–August median OSAVI of each summer")
    if not status.get("season_complete", True):
        st.caption(f"The {season} season is still in progress; numbers can change until 31 August.")
    if status.get("result") != "ok":
        st.warning(f"Latest scan: {status.get('result')}")

    legend([(f"background:{s['fill']};border:1px solid {s['line']}", s["label"]) for s in
            sorted(STATUS.values(), key=lambda s: -s["order"])]
           + [("border:2px dashed #374151", "Dashed outline: parcel-shaped patch, possibly trees removed")])

    w, so, e, n = r["bbox"]
    m = folium.Map(location=[(so + n) / 2, (w + e) / 2], zoom_start=13, tiles="OpenStreetMap")
    folium.Rectangle([[so, w], [n, e]], color="#2563EB", weight=1, fill=False, tooltip="Scanned area").add_to(m)
    add_zones(m, zones)
    near = [x for x in recs if so - 0.3 <= x["lat"] <= n + 0.3 and w - 0.3 <= x["lon"] <= e + 0.3]
    add_records(m, near, 9999, -1)
    m.fit_bounds([[so, w], [n, e]])
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, width="100%", height=620, returned_objects=[])

    if history:
        df = pd.DataFrame(history).set_index("season")
        if {"new_ha", "again_ha"} <= set(df.columns):
            st.markdown("**Flagged area per summer** (hectares; areas that greened up again the following summer are removed)")
            chart = df[["new_ha", "again_ha"]].rename(columns={"new_ha": "New", "again_ha": "Declining again"})
            st.bar_chart(chart, color=[STATUS["new"]["fill"], STATUS["continuing"]["fill"]])

    share = status.get("baseline_share_in_range")
    masks = status.get("masks", {})
    st.caption(
        f"Scanned {status.get('scan_date')} (last checked {status.get('last_checked')}). "
        f"Land-cover filter: {masks.get('worldcover', 'n/a')}"
        + (f"; olive-grove mask: {masks['olive_mask']}" if masks.get("olive_mask") else "")
        + (f". {share:.0%} of judged cells had a baseline in the olive range." if share is not None else "."))


def method_notes():
    with st.expander("How the satellite scan works, and its limits"):
        st.markdown(
            "- Each summer (1 July – 31 August) the scan takes the median **OSAVI** greenness of every 10 m "
            "Sentinel-2 cell and compares it with the summer before.\n"
            "- A cell is flagged when greenness falls by at least 30 % and 0.04 from an olive-like level. Cells are "
            "ignored if they are built-up or next to buildings, far greener than an olive crown (irrigated crops), "
            "or much greener than two summers earlier (rotating crops).\n"
            "- Olive trees hit by Xylella do not recover. A cell that wins back half its loss the next summer "
            "(pruning, drought, mowing) is dropped from the record.\n"
            "- **At 10 m only advanced decline shows.** Early or symptomless infection needs airborne hyperspectral "
            "and thermal data (Zarco-Tejada et al. 2018; D'Addabbo et al. 2025).\n"
            "- A flagged area is a lead for a field check, not a diagnosis. Only laboratory tests by the plant-health "
            "service confirm Xylella.")


def main():
    st.title("Xylella Watch")
    st.markdown("Satellite signs of olive-grove decline, with official *Xylella fastidiosa* records across the Mediterranean.")
    regs, recs = regions(), records()
    views = ["Mediterranean overview"] + [r["name"] for r in regs]
    choice = st.sidebar.radio("View", views)
    if choice == views[0]:
        overview(regs, recs)
    else:
        region_view(next(r for r in regs if r["name"] == choice), recs)
    method_notes()
    st.caption("Imagery: Copernicus Sentinel-2 (ESA) via Microsoft Planetary Computer · Land cover: ESA WorldCover 2021 · "
               "Official records: EPPO, European Commission, regional plant-health services and reporting of their "
               "findings (sources linked in each marker).")


main()
