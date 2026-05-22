import re
import os
import io
import streamlit as st
import pandas as pd
import plotly.express as px
import gspread
from google.oauth2.service_account import Credentials

# --- 1. PAGE CONFIGURATION ---
st.set_page_config(page_title="Model Progress Dashboard", layout="wide")

st.markdown("""
    <style>
    #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
    div[data-testid="stToolbar"] {visibility: hidden;}
    /* Tighten up radio button row spacing */
    div[data-testid="stHorizontalBlock"] > div { gap: 0.25rem; }
    </style>
""", unsafe_allow_html=True)

# --- 2. SECURITY GATE ---
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

url_password = st.query_params.get("pwd", "")
secret_password = st.secrets.get("DASHBOARD_PASSWORD", "fallback_local_password")

if url_password == secret_password:
    st.session_state["authenticated"] = True

if not st.session_state["authenticated"]:
    st.title("🔒 Private Operational Dashboard")
    user_input = st.text_input("Enter Access Password:", type="password")
    if user_input == secret_password:
        st.session_state["authenticated"] = True
        st.rerun()
    else:
        if user_input:
            st.error("Invalid credentials.")
        st.warning("This directory is restricted. Please authenticate to view.")
        st.stop()

# --- 3. DATA FETCHING & CACHING ---
@st.cache_data(ttl=600)
def load_data():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if os.path.exists("creds.json"):
        creds = Credentials.from_service_account_file("creds.json", scopes=scopes)
    elif "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    else:
        st.error("Missing Google Credentials.")
        st.stop()
    client = gspread.authorize(creds)
    SHEET_URL = "https://docs.google.com/spreadsheets/d/1RE039NcnPeQtQrvI5zjLyADzAr-ZseBPUq388SxkV-Y/edit"
    sheet = client.open_by_url(SHEET_URL).worksheets()[1]
    raw_data = sheet.get_all_values()
    df = pd.DataFrame(raw_data[1:], columns=raw_data[0])
    df.columns = ["Model Name", "Date", "KPI"]
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce').dt.date
    df['KPI'] = pd.to_numeric(df['KPI'], errors='coerce')
    df = df.dropna(subset=['Date', 'KPI'])
    df['Project Code'] = df['Model Name'].str.extract(r'(D\d{6})')
    df['Project Code'] = df['Project Code'].fillna('Standalone/Other')
    overlap_groups = df.groupby(['Date', 'KPI'])['Model Name'].transform(lambda x: '<br> • '.join(sorted(x.unique())))
    df['Overlapping Models'] = '• ' + overlap_groups
    df['Overlap Count'] = df.groupby(['Date', 'KPI'])['Model Name'].transform('nunique')
    df = df.sort_values(by=['Model Name', 'Date'])
    return df

df = load_data()

# --- 4. STATE MANAGEMENT ---
if "sidebar_is_open" not in st.session_state:
    st.session_state["sidebar_is_open"] = False

def save_settings():
    if 'ui_jitter'   in st.session_state: st.session_state.saved_jitter   = st.session_state.ui_jitter
    if 'ui_yscale'   in st.session_state: st.session_state.saved_yscale   = st.session_state.ui_yscale
    if 'ui_projects' in st.session_state: st.session_state.saved_projects = st.session_state.ui_projects
    if 'ui_models'   in st.session_state: st.session_state.saved_models   = st.session_state.ui_models
    if 'ui_ystretch' in st.session_state: st.session_state.saved_ystretch = st.session_state.ui_ystretch

if 'saved_jitter'   not in st.session_state: st.session_state.saved_jitter   = False
if 'saved_projects' not in st.session_state: st.session_state.saved_projects = []
if 'saved_models'   not in st.session_state: st.session_state.saved_models   = []
if 'saved_yscale'   not in st.session_state: st.session_state.saved_yscale   = "Linear"
if 'saved_ystretch' not in st.session_state: st.session_state.saved_ystretch = 5.0

# Legend mode: hidden by default so chart is compact; user can expand
if 'legend_mode' not in st.session_state:
    st.session_state.legend_mode = "🚫 Hidden"

# Settings open/close toggle
if not st.session_state["sidebar_is_open"]:
    if st.button("📂 Open Settings"):
        st.session_state["sidebar_is_open"] = True
        st.rerun()
else:
    if st.button("📁 Close Settings"):
        st.session_state["sidebar_is_open"] = False
        st.rerun()

# --- 5. LAYOUT & FILTERS ---
if st.session_state["sidebar_is_open"]:
    settings_col, chart_col = st.columns([1, 4], gap="large")
    with settings_col:
        st.header("Graph Settings")
        st.checkbox(
            "⚡ Separate Overlapping Points",
            value=st.session_state.saved_jitter,
            key="ui_jitter",
            on_change=save_settings
        )
        st.radio(
            "📐 Y-Axis Scale",
            options=["Linear", "Log", "From Zero"],
            index=["Linear", "Log", "From Zero"].index(st.session_state.saved_yscale if st.session_state.saved_yscale in ["Linear", "Log", "From Zero"] else "Linear"),
            key="ui_yscale",
            on_change=save_settings,
            help="Linear: fits to data range. Log: spreads clustered lines. From Zero: anchors axis at 0.",
        )
        st.slider(
            "📏 Chart Height Limit",
            min_value=1.0,
            max_value=8.0,
            step=0.5,
            value=float(st.session_state.saved_ystretch),
            key="ui_ystretch",
            on_change=save_settings,
            help=(
                "Controls how tall the models chart can grow as more lines are added. "
                "1× caps at ~500 px; 8× allows up to ~4 000 px. "
                "A taller chart gives more vertical pixels per KPI unit, "
                "visually separating clustered lines."
            ),
        )
        st.markdown("---")
        st.subheader("🔍 Filters")
        all_projects = sorted(df['Project Code'].unique())
        st.multiselect(
            "Filter by Project Code:",
            options=all_projects,
            default=st.session_state.saved_projects,
            key="ui_projects",
            on_change=save_settings
        )
        filtered_df_temp = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
        all_models = sorted(filtered_df_temp['Model Name'].unique())
        st.multiselect(
            "Filter by Specific Model:",
            options=all_models,
            default=st.session_state.saved_models,
            key="ui_models",
            on_change=save_settings
        )
else:
    chart_col = st.container()

# --- 6. FILTER APPLICATION ---
filtered_df = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
final_df    = filtered_df if not st.session_state.saved_models else filtered_df[filtered_df['Model Name'].isin(st.session_state.saved_models)]

# Reset Y-range sliders whenever the active filter set changes so the defaults
# recalculate from the new data instead of keeping a stale position.
_filter_sig = str(st.session_state.saved_projects) + str(st.session_state.saved_models)
if st.session_state.get('_last_filter_sig') != _filter_sig:
    st.session_state['_last_filter_sig'] = _filter_sig
    for _k in ('y_range_slider_models', 'y_range_slider_sum'):
        if _k in st.session_state:
            del st.session_state[_k]

# --- 7. LEGEND MODE HELPER ---
LEGEND_OPTIONS = ["🚫 Hidden", "📁 By Project", "📋 All Models"]

# --- 7b. CHART HEIGHT HELPER ---
def compute_chart_height(n_models, stretch_limit, legend_visible):
    """
    Grows chart height with model count so each line gets more vertical pixels.
    stretch_limit (1–8) controls the ceiling: 1× → 500 px, 8× → 4 000 px.
    """
    max_h   = int(stretch_limit * 500)          # 500 px … 4 000 px
    per_model = 30 if legend_visible else 22
    target  = 300 + n_models * per_model
    return max(480, min(target, max_h))


# --- 7c. AUTO Y-RANGE HELPER ---
def compute_auto_yrange(kpi_series):
    """
    Tight fit around the data with ~8 % padding each side.
    Used as the initial default for the Y-range slider.
    """
    vals = kpi_series.dropna()
    if vals.empty:
        return [0.0, 1.0]
    lo, hi = float(vals.min()), float(vals.max())
    pad = max((hi - lo) * 0.08, abs(hi) * 0.02, 1e-6)
    return [max(0.0, lo - pad), hi + pad]


def apply_legend(fig, mode, inside=True):
    """
    Configure legend visibility and grouping on a Plotly figure.

    Modes:
      🚫 Hidden    – showlegend=False, no overhead
      📁 By Project – one legend entry per D###### project code; clicking
                      toggles all models in that project simultaneously.
                      Individual model names are still visible on hover.
      📋 All Models – every model listed, grouped under project headers
                      with collapsible group titles.

    When inside=True the legend floats as a semi-transparent overlay in
    the top-right corner of the plot area with a scrollable maxheight,
    so it never pushes the chart wider or taller.
    """
    if mode == "🚫 Hidden":
        fig.update_layout(showlegend=False)
        return

    # Shared overlay legend style (inside plot, scrollable)
    legend_cfg = dict(
        x=0.01, y=0.99,
        xanchor="left", yanchor="top",
        bgcolor="rgba(20,20,20,0.82)",
        bordercolor="rgba(180,180,180,0.35)",
        borderwidth=1,
        font=dict(size=13),
        tracegroupgap=3,
        itemsizing="constant",
        maxheight=420,          # enables scroll when list overflows
    ) if inside else dict(
        yanchor="top", y=0.99, xanchor="left", x=0.01,
        font=dict(size=13),
        tracegroupgap=3,
        itemsizing="constant",
    )

    fig.update_layout(showlegend=True, legend=legend_cfg)

    seen_projects = {}  # project_code -> first trace index

    for trace in fig.data:
        match = re.match(r'(D\d{6})', trace.name)
        p_code = match.group(1) if match else "Standalone/Other"

        if mode == "📁 By Project":
            trace.legendgroup = p_code
            if p_code not in seen_projects:
                seen_projects[p_code] = True
                # Show only ONE entry per project; label it as the project code
                trace.showlegend = True
                trace.name = f"📁 {p_code}"
            else:
                trace.showlegend = False

        elif mode == "📋 All Models":
            trace.legendgroup = p_code
            trace.legendgrouptitle = dict(text=f"📁 {p_code}", font=dict(size=13))
            trace.showlegend = True


# --- 8. VISUALIZATION ---
view_mode = st.query_params.get("view", "all").lower()

with chart_col:
    if final_df.empty:
        st.warning("No data available for the selected filters.")
    else:

        # ── CHART 1: INDIVIDUAL MODELS ────────────────────────────────────
        if view_mode in ["all", "models"]:

            # Legend mode selector lives above the chart title, aligned right
            title_col, legend_ctl_col = st.columns([3, 2])
            with title_col:
                st.title("KPI per Model Over Time")
            with legend_ctl_col:
                st.write("")   # small vertical nudge
                chosen = st.radio(
                    "Legend",
                    options=LEGEND_OPTIONS,
                    index=LEGEND_OPTIONS.index(st.session_state.legend_mode),
                    horizontal=True,
                    key="legend_radio_1",
                    label_visibility="collapsed",
                )
                st.session_state.legend_mode = chosen

            unique_model_count = len(final_df['Model Name'].unique())
            _legend_vis = st.session_state.legend_mode != "🚫 Hidden"

            # Height grows with model count so lines get more vertical pixels
            calculated_height = compute_chart_height(
                unique_model_count,
                float(st.session_state.saved_ystretch),
                _legend_vis
            )

            # Jitter: offset scales with data range so it's always visible
            final_df = final_df.copy()
            final_df['Display KPI'] = final_df['KPI']
            if st.session_state.saved_jitter:
                kpi_range = max(final_df["KPI"].max() - final_df["KPI"].min(), 0.01)
                jitter_scale = kpi_range * 0.008
                final_df["Display KPI"] = (
                    final_df["KPI"]
                    + final_df.groupby(["Date", "KPI"]).cumcount() * jitter_scale
                )

            # ── Y-AXIS RANGE SLIDER ──────────────────────────────────────────
            # Explicit two-handle slider so the user can stretch/compress the Y
            # axis the same way the X rangeslider works below the chart.
            _auto_yr   = compute_auto_yrange(final_df['Display KPI'])
            _y_max_cap = max(round(float(final_df['Display KPI'].max()) * 3.0, 5),
                             _auto_yr[1] * 1.5)
            _y_step    = max(0.000001, round(_y_max_cap / 2000, 7))
            _yscale    = st.session_state.saved_yscale

            if _yscale == "Linear":
                y_range_models = st.slider(
                    "↕️ Y-Axis Range",
                    min_value=0.0,
                    max_value=float(_y_max_cap),
                    value=(float(_auto_yr[0]), float(_auto_yr[1])),
                    step=float(_y_step),
                    format="%.5f",
                    key="y_range_slider_models",
                    help="Drag either handle to zoom the Y-axis in or out.",
                )
            # ────────────────────────────────────────────────────────────────

            fig_models = px.line(
                final_df,
                x="Date", y="Display KPI",
                color="Model Name",
                markers=True,
                height=calculated_height,
                custom_data=["Model Name", "KPI", "Overlapping Models", "Overlap Count"]
            )
            fig_models.update_traces(
                hovertemplate=(
                    "<b>🎯 Model:</b> %{customdata[0]}<br>"
                    "<b>📅 Date:</b> %{x}<br>"
                    "<b>📈 KPI:</b> %{customdata[1]:.4f}<br>"
                    "───────────────────────<br>"
                    "<b>👥 All at this point (%{customdata[3]}):</b><br>"
                    "%{customdata[2]}<extra></extra>"
                ),
                hoverlabel_namelength=-1,
                line=dict(width=2.5),
                marker=dict(size=7, line=dict(width=1, color="white")),
            )
            fig_models.update_layout(
                xaxis_title="<b>Date</b>",
                yaxis_title="<b>KPI</b>",
                legend_title="Active Models",
                margin=dict(l=85, r=20, t=30, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left"),
            )

            # Apply legend mode (grouped / full / hidden)
            apply_legend(fig_models, st.session_state.legend_mode, inside=True)

            fig_models.update_xaxes(
                type="date",
                tickformat="%b %d, %Y",
                tickangle=-40,
                tickmode="auto",
                nticks=14,
                automargin=True,
                # yaxis=dict(rangemode="match") tells the mini rangeslider chart
                # to mirror the main y-axis scale, eliminating the ghost line.
                rangeslider=dict(visible=True, thickness=0.04,
                                 yaxis=dict(rangemode="match")),
            )
            fig_models.update_yaxes(
                automargin=True,
                fixedrange=False,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            if _yscale == "Linear":
                fig_models.update_yaxes(range=list(y_range_models))

            st.plotly_chart(fig_models, use_container_width=True,
                            config={"scrollZoom": True})

            if view_mode == "all":
                st.markdown("---")

        # ── CHART 2: SUMMATION ────────────────────────────────────────────
        if view_mode in ["all", "summation"]:
            st.title("KPI Summation")
            sum_df  = final_df.groupby("Date", as_index=False)["KPI"].sum()

            # ── Y-AXIS RANGE SLIDER ──────────────────────────────────────────
            _yscale      = st.session_state.saved_yscale
            _auto_yr_sum = compute_auto_yrange(sum_df['KPI'])
            _y_max_sum   = max(round(float(sum_df['KPI'].max()) * 3.0, 5),
                               _auto_yr_sum[1] * 1.5)
            _y_step_sum  = max(0.000001, round(_y_max_sum / 2000, 7))

            if _yscale == "Linear":
                y_range_sum = st.slider(
                    "↕️ Y-Axis Range",
                    min_value=0.0,
                    max_value=float(_y_max_sum),
                    value=(float(_auto_yr_sum[0]), float(_auto_yr_sum[1])),
                    step=float(_y_step_sum),
                    format="%.5f",
                    key="y_range_slider_sum",
                    help="Drag either handle to zoom the Y-axis in or out.",
                )
            # ────────────────────────────────────────────────────────────────

            fig_sum = px.line(sum_df, x="Date", y="KPI", markers=True, height=500)
            fig_sum.update_traces(
                hovertemplate=(
                    "<b>📅 Date:</b> %{x}<br>"
                    "<b>📈 Aggregate KPI:</b> %{y:.4f}<br>"
                    "<extra></extra>"
                ),
                line=dict(width=2.5, color="#1f77b4"),
                marker=dict(size=8, color="#1f77b4", line=dict(width=1.5, color="white")),
            )
            fig_sum.update_layout(
                xaxis_title="<b>Date</b>",
                yaxis_title="<b>Total KPI</b>",
                showlegend=False,
                margin=dict(l=85, r=20, t=30, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left"),
            )
            fig_sum.update_xaxes(
                type="date",
                tickformat="%b %d, %Y",
                tickangle=-40,
                tickmode="auto",
                nticks=14,
                automargin=True,
                rangeslider=dict(visible=True, thickness=0.04,
                                 yaxis=dict(rangemode="match")),
            )
            fig_sum.update_yaxes(
                automargin=True,
                fixedrange=False,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            if _yscale == "Linear":
                fig_sum.update_yaxes(range=list(y_range_sum))

            st.plotly_chart(fig_sum, use_container_width=True,
                            config={"scrollZoom": True})
