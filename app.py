import re
import os
import io
import datetime
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
    /* Pull chart up — the t=140 plotly margin already reserves tooltip space
       above the plot area, so we only need a small CSS nudge here. */
    div[data-testid="stPlotlyChart"] { margin-top: -1rem; }
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

if 'saved_jitter'   not in st.session_state: st.session_state.saved_jitter   = False
if 'saved_projects' not in st.session_state: st.session_state.saved_projects = []
if 'saved_models'   not in st.session_state: st.session_state.saved_models   = []
if 'saved_yscale'   not in st.session_state: st.session_state.saved_yscale   = "Linear"

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

# Weekdays only (Mon–Fri). Removing weekends here means neither chart needs
# rangebreaks, avoiding the rangebreaks+rangeslider conflict that blanked chart 1.
final_df = final_df[pd.to_datetime(final_df['Date']).dt.dayofweek < 5].copy()

# --- 7. LEGEND MODE HELPER ---
LEGEND_OPTIONS = ["🚫 Hidden", "📁 By Project", "📋 All Models"]

def apply_legend(fig, mode, inside=True):
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
    seen_projects = {}

    for trace in fig.data:
        match = re.match(r'(D\d{6})', trace.name)
        p_code = match.group(1) if match else "Standalone/Other"

        if mode == "📁 By Project":
            trace.legendgroup = p_code
            if p_code not in seen_projects:
                seen_projects[p_code] = True
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
today = datetime.date.today()

with chart_col:
    if final_df.empty:
        st.warning("No data available for the selected filters.")
    else:
        # --- DYNAMIC 3-MONTH DATE WINDOW LOGIC ---
        max_date_ts = pd.to_datetime(final_df['Date']).max()
        max_date = today if pd.isna(max_date_ts) else max_date_ts.date()

        right_bound_if_centered = today + datetime.timedelta(days=44)
        
        if right_bound_if_centered < max_date:
            # The 3-month window centered on today doesn't reach the end of the data
            x_start = today - datetime.timedelta(days=44)
            x_end = today + datetime.timedelta(days=44)
        else:
            # The data ends sooner, anchor strictly to the end of the graph + 1 day to prevent cutoff
            x_end = max_date + datetime.timedelta(days=1)
            x_start = x_end - datetime.timedelta(days=88)


        # ── CHART 1: INDIVIDUAL MODELS ────────────────────────────────────
        if view_mode in ["all", "models"]:

            title_col, legend_ctl_col = st.columns([3, 2])
            with title_col:
                st.title("KPI per Model Over Time")
            with legend_ctl_col:
                st.write("")
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
            # Height scales with model count so lines get more vertical pixels as
            # the chart grows. Clamped to a sensible range for the ClickUp embed.
            calculated_height = max(480, min(900, 300 + unique_model_count * 22))

            final_df = final_df.copy()
            final_df['Display KPI'] = final_df['KPI']
            if st.session_state.saved_jitter:
                kpi_range = max(final_df["KPI"].max() - final_df["KPI"].min(), 0.01)
                jitter_scale = kpi_range * 0.008
                final_df["Display KPI"] = (
                    final_df["KPI"]
                    + final_df.groupby(["Date", "KPI"]).cumcount() * jitter_scale
                )

            fig_models = px.line(
                final_df,
                x="Date", y="Display KPI",
                color="Model Name",
                markers=True,
                height=calculated_height - 10,
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
                margin=dict(l=85, r=20, t=140, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1),
            )

            apply_legend(fig_models, st.session_state.legend_mode, inside=True)

            # Updated dynamic axes mapping using nticks logic
            fig_models.update_xaxes(
                type="date",
                tickmode="auto",           
                tickformat="%b %d",        
                nticks=90,                 
                tickangle=-40,
                automargin=True,
                range=[x_start, x_end],
                rangeslider=dict(visible=True, thickness=0.04, yaxis=dict(rangemode="match")),
            )
            
            _yscale = st.session_state.saved_yscale
            fig_models.update_yaxes(
                automargin=True,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            st.plotly_chart(fig_models, use_container_width=True)

            if view_mode == "all":
                st.markdown("---")

        # ── CHART 2: SUMMATION ────────────────────────────────────────────
        if view_mode in ["all", "summation"]:
            st.title("KPI Summation")
            sum_df  = final_df.groupby("Date", as_index=False)["KPI"].sum()
            fig_sum = px.line(sum_df, x="Date", y="KPI", markers=True, height=calculated_height)
            fig_sum.update_traces(
                hovertemplate=(
                    "<b>📅 Date:</b> %{x}<br>"
                    "<b>📈 Aggregate KPI:</b> %{y:.4f}<br>"
                    "<extra></extra>"
                ),
                connectgaps=True,
                line=dict(width=2.5, color="#1f77b4"),
                marker=dict(size=8, color="#1f77b4", line=dict(width=1.5, color="white")),
            )
            fig_sum.update_layout(
                xaxis_title="<b>Date</b>",
                yaxis_title="<b>Total KPI</b>",
                showlegend=False,
                margin=dict(l=85, r=20, t=140, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1),
            )
            
            # Updated dynamic axes mapping using nticks logic
            fig_sum.update_xaxes(
                type="date",
                tickmode="auto",
                tickformat="%b %d",
                nticks=90,
                tickangle=-40,
                automargin=True,
                range=[x_start, x_end],
                rangeslider=dict(visible=False),
            )
            
            _yscale = st.session_state.saved_yscale
            fig_sum.update_yaxes(
                automargin=True,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            st.plotly_chart(fig_sum, use_container_width=True)
