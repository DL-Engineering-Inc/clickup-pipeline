"""
app.py — Model KPI Progress Dashboard (Streamlit)

Provides a password-protected, interactive web dashboard for visualising
model-level KPI metrics over time.  Data is sourced from two Google Sheets tabs:

  Sheet 2 — Weighted difficulty KPI  (difficulty_factor / duration)
  Sheet 3 — Flat unweighted KPI      (1 / duration)

Three chart modes are available, selectable via the ?view= query parameter:
  models      — individual per-model time-series lines, one chart per KPI framework
  summation   — aggregate KPI sum across all visible models (dual-series overlay)
  all         — all charts displayed in sequence (default)

Display settings (project/model filters, Y-axis scale, jitter, legend mode) are
persisted in Streamlit session state so that period-navigation reruns do not reset
user preferences.  KPI data is cached for 10 minutes (ttl=600) to reduce
Google Sheets API calls.

Authentication: passwords are checked against a secret stored in st.secrets
(DASHBOARD_PASSWORD).  A URL query parameter (?pwd=) is also accepted for
embedded access.

Dependencies: streamlit, gspread, google-auth, pandas, plotly
"""
import re
import os
import io
import datetime
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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
    /* Small top gap — ensures the Plotly toolbar isn't clipped by the iframe edge */
    div[data-testid="stPlotlyChart"] { margin-top: 0.25rem; }
    /* Keep period navigation buttons compact and visually tight */
    div[data-testid="stButton"] > button { padding: 0.2rem 0.7rem; }
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
    """
    Downloads, normalizes, and structures model KPI datasets from Sheets 2 and 3 concurrently.
    """
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
    
    # Extract Tab 2 (Weighted Difficulty KPI Framework)
    sheet2 = client.open_by_url(SHEET_URL).worksheets()[1]
    raw_data2 = sheet2.get_all_values()
    df2 = pd.DataFrame(raw_data2[1:], columns=raw_data2[0])
    df2.columns = ["Model Name", "Date", "KPI"]
    df2['Date'] = pd.to_datetime(df2['Date'], errors='coerce').dt.date
    df2['KPI'] = pd.to_numeric(df2['KPI'], errors='coerce')
    df2 = df2.dropna(subset=['Date', 'KPI'])
    df2['Project Code'] = df2['Model Name'].str.extract(r'(D\d{6})')
    df2['Project Code'] = df2['Project Code'].fillna('Standalone/Other')
    overlap_groups2 = df2.groupby(['Date', 'KPI'])['Model Name'].transform(lambda x: '<br> • '.join(sorted(x.unique())))
    df2['Overlapping Models'] = '• ' + overlap_groups2
    df2['Overlap Count'] = df2.groupby(['Date', 'KPI'])['Model Name'].transform('nunique')
    df2 = df2.sort_values(by=['Model Name', 'Date'])

    # Extract Tab 3 (Flat Unweighted 1/Duration KPI Framework)
    sheet3 = client.open_by_url(SHEET_URL).worksheets()[2]
    raw_data3 = sheet3.get_all_values()
    df3 = pd.DataFrame(raw_data3[1:], columns=raw_data3[0])
    df3.columns = ["Model Name", "Date", "KPI"]
    df3['Date'] = pd.to_datetime(df3['Date'], errors='coerce').dt.date
    df3['KPI'] = pd.to_numeric(df3['KPI'], errors='coerce')
    df3 = df3.dropna(subset=['Date', 'KPI'])
    df3['Project Code'] = df3['Model Name'].str.extract(r'(D\d{6})')
    df3['Project Code'] = df3['Project Code'].fillna('Standalone/Other')
    overlap_groups3 = df3.groupby(['Date', 'KPI'])['Model Name'].transform(lambda x: '<br> • '.join(sorted(x.unique())))
    df3['Overlapping Models'] = '• ' + overlap_groups3
    df3['Overlap Count'] = df3.groupby(['Date', 'KPI'])['Model Name'].transform('nunique')
    df3 = df3.sort_values(by=['Model Name', 'Date'])

    return df2, df3

df, df_flat = load_data()
today = datetime.date.today()

# --- 4. STATE MANAGEMENT ---
if "sidebar_is_open" not in st.session_state:
    st.session_state["sidebar_is_open"] = False

def save_settings():
    """
    Persists active widget values to durable session-state keys.

    Streamlit resets widget state on rerun unless values are explicitly mirrored
    to independent session-state keys.  This callback is wired to each settings
    widget's on_change event so that period-navigation reruns (which trigger a
    st.rerun()) do not discard the user's current filter selections.
    """
    if 'ui_jitter'   in st.session_state: st.session_state.saved_jitter   = st.session_state.ui_jitter
    if 'ui_yscale'   in st.session_state: st.session_state.saved_yscale   = st.session_state.ui_yscale
    if 'ui_projects' in st.session_state: st.session_state.saved_projects = st.session_state.ui_projects
    if 'ui_models'   in st.session_state: st.session_state.saved_models   = st.session_state.ui_models

if 'saved_jitter'   not in st.session_state: st.session_state.saved_jitter   = False
if 'saved_projects' not in st.session_state: st.session_state.saved_projects = []
if 'saved_models'   not in st.session_state: st.session_state.saved_models   = []
if 'saved_yscale'   not in st.session_state: st.session_state.saved_yscale   = "Linear"

# Time interval tracking parameters across views
if 'time_view'      not in st.session_state: st.session_state.time_view      = "Month"
if 'period_offset'  not in st.session_state: st.session_state.period_offset  = 0

if 'time_view2'     not in st.session_state: st.session_state.time_view2     = "Month"
if 'period_offset2' not in st.session_state: st.session_state.period_offset2 = 0

if 'time_view3'     not in st.session_state: st.session_state.time_view3     = "Month"
if 'period_offset3' not in st.session_state: st.session_state.period_offset3 = 0

def set_view(v):
    if st.session_state.time_view != v:
        st.session_state.time_view     = v
        st.session_state.period_offset = 0
        st.rerun()

def set_view2(v):
    if st.session_state.time_view2 != v:
        st.session_state.time_view2     = v
        st.session_state.period_offset2 = 0
        st.rerun()

def set_view3(v):
    if st.session_state.time_view3 != v:
        st.session_state.time_view3     = v
        st.session_state.period_offset3 = 0
        st.rerun()

def get_period_bounds(view, offset, today):
    """Calculates chronological tracking limits based on frame configuration state."""
    td = datetime.timedelta
    if view == "Week":
        monday = today - td(days=today.weekday())
        start  = monday + td(weeks=offset)
        end    = start  + td(days=6)
        return start, end
    if view == "Month":
        raw_month = today.month - 1 + offset
        year  = today.year + raw_month // 12
        month = raw_month % 12 + 1
        start = datetime.date(year, month, 1)
        if month == 12:
            end = datetime.date(year + 1, 1, 1) - td(days=1)
        else:
            end = datetime.date(year, month + 1, 1) - td(days=1)
        return start, end
    if view == "Quarter":
        cur_q    = (today.month - 1) // 3
        total_q  = today.year * 4 + cur_q + offset
        q_year   = total_q // 4
        q_num    = total_q % 4
        q_m_start = q_num * 3 + 1
        start    = datetime.date(q_year, q_m_start, 1)
        q_m_end  = q_m_start + 2
        if q_m_end == 12:
            end = datetime.date(q_year + 1, 1, 1) - td(days=1)
        else:
            end = datetime.date(q_year, q_m_end + 1, 1) - td(days=1)
        return start, end
    return None, None

if 'legend_mode' not in st.session_state:
    st.session_state.legend_mode = "🚫 Hidden"

if 'legend_mode3' not in st.session_state:
    st.session_state.legend_mode3 = "🚫 Hidden"

# --- 5. LAYOUT & FILTERS ---
if st.session_state["sidebar_is_open"]:
    settings_col, chart_col = st.columns([1, 4], gap="large")
    with settings_col:
        st.header("Graph Settings")
        st.checkbox("⚡ Separate Overlapping Points", value=st.session_state.saved_jitter, key="ui_jitter", on_change=save_settings)
        st.radio(
            "📐 Y-Axis Scale",
            options=["Linear", "Log", "From Zero"],
            index=["Linear", "Log", "From Zero"].index(st.session_state.saved_yscale if st.session_state.saved_yscale in ["Linear", "Log", "From Zero"] else "Linear"),
            key="ui_yscale",
            on_change=save_settings,
        )
        st.markdown("---")
        st.subheader("🔍 Filters")
        all_projects = sorted(df['Project Code'].unique())
        st.multiselect("Filter by Project Code:", options=all_projects, default=st.session_state.saved_projects, key="ui_projects", on_change=save_settings)
        filtered_df_temp = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
        all_models = sorted(filtered_df_temp['Model Name'].unique())
        st.multiselect("Filter by Specific Model:", options=all_models, default=st.session_state.saved_models, key="ui_models", on_change=save_settings)
else:
    chart_col = st.container()

# --- 6. FILTER APPLICATION ---
filtered_df = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
final_df    = filtered_df if not st.session_state.saved_models else filtered_df[filtered_df['Model Name'].isin(st.session_state.saved_models)]
final_df    = final_df[pd.to_datetime(final_df['Date']).dt.dayofweek < 5].copy()

filtered_df_flat = df_flat if not st.session_state.saved_projects else df_flat[df_flat['Project Code'].isin(st.session_state.saved_projects)]
final_df_flat    = filtered_df_flat if not st.session_state.saved_models else filtered_df_flat[filtered_df_flat['Model Name'].isin(st.session_state.saved_models)]
final_df_flat    = final_df_flat[pd.to_datetime(final_df_flat['Date']).dt.dayofweek < 5].copy()

_data_min = pd.to_datetime(final_df['Date']).min().date() if not final_df.empty else today
_data_max = pd.to_datetime(final_df['Date']).max().date() if not final_df.empty else today

# --- 7. LEGEND MODE HELPER ---
LEGEND_OPTIONS = ["🚫 Hidden", "📁 By Project", "📋 All Models"]

def apply_legend(fig, mode, inside=True):
    """
    Applies a legend configuration to a Plotly figure based on the selected display mode.

    Args:
        fig:    Plotly Figure object to modify in place.
        mode:   One of LEGEND_OPTIONS — "🚫 Hidden", "📁 By Project", or "📋 All Models".
                Hidden removes the legend entirely.  By Project collapses all traces for
                a project code into a single legend entry.  All Models shows each trace
                individually, grouped under its project code header.
        inside: If True, positions the legend overlay inside the plot area; if False,
                uses Plotly's default external positioning.
    """
    if mode == "🚫 Hidden":
        fig.update_layout(showlegend=False)
        return

    legend_cfg = dict(
        x=0.01, y=0.99,
        xanchor="left", yanchor="top",
        bgcolor="rgba(20,20,20,0.82)",
        bordercolor="rgba(180,180,180,0.35)",
        borderwidth=1,
        font=dict(size=13),
        tracegroupgap=3,
        itemsizing="constant",
        maxheight=420,
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
VIEW_ORDER = ["Week", "Month", "Quarter", "All Time"]

def get_nticks(view):
    """Returns the target x-axis tick count for a given time-view granularity."""
    return {"Week": 5, "Month": 23, "Quarter": 14, "All Time": 20}.get(view, 20)

def inline_title_nav(title, view_key, offset_key, set_fn, btn_prefix, data_min, data_max, extra_col=None):
    """
    Renders a single-row chart header combining a title, optional extra widget,
    time-view selector buttons (Week / Month / Quarter / All Time), and
    Prev / Next navigation arrows.

    Args:
        title:      Chart heading string displayed at the left of the row.
        view_key:   Session-state key holding the current time-view label.
        offset_key: Session-state key holding the signed period offset from today.
        set_fn:     Callable(label) — invoked when a view button is clicked.
        btn_prefix: Unique string prefix for all Streamlit button keys in this row.
        data_min:   Earliest date present in the filtered dataset (disables Prev
                    when the preceding period falls entirely before this date).
        data_max:   Latest date present in the filtered dataset (disables Next
                    when the following period starts after this date).
        extra_col:  Optional (width, callable) tuple for injecting an additional
                    widget (e.g. legend mode selector) between the title and the
                    view buttons.

    Returns:
        Tuple (x_start, x_end, nticks) — the date range and tick count to apply
        to the chart's x-axis for the currently selected period.
    """
    _v = st.session_state[view_key]
    _o = st.session_state[offset_key]

    if _v == "All Time":
        can_prev, can_next = False, False
    else:
        _, prev_e = get_period_bounds(_v, _o - 1, today)
        next_s, _ = get_period_bounds(_v, _o + 1, today)
        can_prev = prev_e >= data_min
        can_next = next_s <= data_max

    extra_w  = extra_col[0] if extra_col else 0
    title_w  = 2.2
    spacer_w = 1.0
    btn_ws   = [0.75, 0.85, 1.0, 1.1]
    nav_ws   = [0.85, 0.85]
    widths   = [title_w] + ([extra_w] if extra_col else []) + [spacer_w] + btn_ws + nav_ws
    cols     = st.columns(widths, gap="small")
    ci = 0

    with cols[ci]: st.markdown(f"### {title}")
    ci += 1

    if extra_col:
        with cols[ci]: extra_col[1]()
        ci += 1

    ci += 1  

    for label in VIEW_ORDER:
        with cols[ci]:
            if st.button(label, key=f"{btn_prefix}_{label}", type="primary" if _v == label else "secondary"): set_fn(label)
        ci += 1

    with cols[ci]:
        if st.button("◀ Prev", key=f"{btn_prefix}_prev", disabled=not can_prev):
            st.session_state[offset_key] -= 1
            st.rerun()
    ci += 1
    with cols[ci]:
        if st.button("Next ▶", key=f"{btn_prefix}_next", disabled=not can_next):
            st.session_state[offset_key] += 1
            st.rerun()

    if _v == "All Time": return data_min - datetime.timedelta(days=1), data_max + datetime.timedelta(days=1), get_nticks(_v)
    xs, xe = get_period_bounds(_v, _o, today)
    return xs, xe, get_nticks(_v)

with chart_col:
    if final_df.empty:
        st.warning("No data available for the selected filters.")
    else:
        unique_model_count = len(final_df['Model Name'].unique())
        calculated_height = max(480, min(900, 300 + unique_model_count * 22))
        
        _yscale = st.session_state.saved_yscale

        # ── CHART 1: INDIVIDUAL MODELS (WEIGHTED DIFFICULTY) ────────────────
        if view_mode in ["all", "models"]:
            def _legend_widget():
                st.write("")
                chosen = st.radio("Legend", options=LEGEND_OPTIONS, index=LEGEND_OPTIONS.index(st.session_state.legend_mode), horizontal=True, key="legend_radio_1", label_visibility="collapsed")
                st.session_state.legend_mode = chosen

            x_start, x_end, _nticks1 = inline_title_nav("KPI per Model Over Time", "time_view", "period_offset", set_view, "c1", _data_min, _data_max, extra_col=(2.0, _legend_widget))

            plot_df = final_df.copy()
            plot_df['Display KPI'] = plot_df['KPI']
            if st.session_state.saved_jitter:
                kpi_range = max(plot_df["KPI"].max() - plot_df["KPI"].min(), 0.01)
                jitter_scale = kpi_range * 0.008
                plot_df["Display KPI"] = plot_df["KPI"] + plot_df.groupby(["Date", "KPI"]).cumcount() * jitter_scale

            fig_models = px.line(plot_df, x="Date", y="Display KPI", color="Model Name", markers=True, height=calculated_height - 10, custom_data=["Model Name", "KPI", "Overlapping Models", "Overlap Count"])
            fig_models.update_traces(
                hovertemplate="<b>🎯 Model:</b> %{customdata[0]}<br><b>📅 Date:</b> %{x}<br><b>📈 KPI:</b> %{customdata[1]:.4f}<br>───────────────────────<br><b>👥 All at this point (%{customdata[3]}):</b><br>%{customdata[2]}<extra></extra>",
                hoverlabel_namelength=-1, line=dict(width=2.5), marker=dict(size=7, line=dict(width=1, color="white"))
            )
            fig_models.update_layout(
                xaxis_title="<b>Date</b>", yaxis_title="<b>KPI</b>", legend_title="Active Models", margin=dict(l=85, r=20, t=50, b=110), hovermode="closest", font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1)
            )

            apply_legend(fig_models, st.session_state.legend_mode, inside=True)
            _tick_kwargs1 = dict(tickmode="auto", nticks=20) if st.session_state.time_view == "All Time" else dict(tickmode="linear", dtick=86400000)
            
            fig_models.update_xaxes(type="date", tickformat="%b %d", tickangle=-40, automargin=True, range=[x_start, x_end], rangeslider=dict(visible=False), **_tick_kwargs1)
            fig_models.update_yaxes(automargin=True, type="log" if _yscale == "Log" else "linear", rangemode="tozero" if _yscale == "From Zero" else "normal", zeroline=False)
            st.plotly_chart(fig_models, width='stretch')

            if view_mode == "all": st.markdown("---")

        # ── CHART 3: RAW INDIVIDUAL MODELS (UNWEIGHTED FLAT) ────────────────
        if view_mode in ["all", "models"]:
            def _legend_widget3():
                st.write("")
                chosen = st.radio("Legend (Raw)", options=LEGEND_OPTIONS, index=LEGEND_OPTIONS.index(st.session_state.legend_mode3), horizontal=True, key="legend_radio_3", label_visibility="collapsed")
                st.session_state.legend_mode3 = chosen

            x_start3, x_end3, _nticks3 = inline_title_nav("Raw KPI per Model Over Time", "time_view3", "period_offset3", set_view3, "c3", _data_min, _data_max, extra_col=(2.0, _legend_widget3))

            plot_df_flat = final_df_flat.copy()
            plot_df_flat['Display KPI'] = plot_df_flat['KPI']
            if st.session_state.saved_jitter:
                kpi_range3 = max(plot_df_flat["KPI"].max() - plot_df_flat["KPI"].min(), 0.01)
                jitter_scale3 = kpi_range3 * 0.008
                plot_df_flat["Display KPI"] = plot_df_flat["KPI"] + plot_df_flat.groupby(["Date", "KPI"]).cumcount() * jitter_scale3

            fig_models3 = px.line(plot_df_flat, x="Date", y="Display KPI", color="Model Name", markers=True, height=calculated_height - 10, custom_data=["Model Name", "KPI", "Overlapping Models", "Overlap Count"])
            fig_models3.update_traces(
                hovertemplate="<b>🎯 Model:</b> %{customdata[0]}<br><b>📅 Date:</b> %{x}<br><b>📈 KPI:</b> %{customdata[1]:.4f}<br>───────────────────────<br><b>👥 All at this point (%{customdata[3]}):</b><br>%{customdata[2]}<extra></extra>",
                hoverlabel_namelength=-1, line=dict(width=2.5), marker=dict(size=7, line=dict(width=1, color="white"))
            )
            fig_models3.update_layout(
                xaxis_title="<b>Date</b>", yaxis_title="<b>Raw KPI</b>", legend_title="Active Models", margin=dict(l=85, r=20, t=50, b=110), hovermode="closest", font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1)
            )

            apply_legend(fig_models3, st.session_state.legend_mode3, inside=True)
            _tick_kwargs3 = dict(tickmode="auto", nticks=20) if st.session_state.time_view3 == "All Time" else dict(tickmode="linear", dtick=86400000)
            
            fig_models3.update_xaxes(type="date", tickformat="%b %d", tickangle=-40, automargin=True, range=[x_start3, x_end3], rangeslider=dict(visible=False), **_tick_kwargs3)
            fig_models3.update_yaxes(automargin=True, type="log" if _yscale == "Log" else "linear", rangemode="tozero" if _yscale == "From Zero" else "normal", zeroline=False)
            st.plotly_chart(fig_models3, width='stretch')

            if view_mode == "all": st.markdown("---")

        # ── CHART 2: SUMMATION (DUAL ACCUMULATION COMPARE) ──────────────────
        if view_mode in ["all", "summation"]:
            x_start2, x_end2, _nticks2 = inline_title_nav("KPI Summation", "time_view2", "period_offset2", set_view2, "c2", _data_min, _data_max)

            sum_df = final_df.groupby("Date", as_index=False)["KPI"].sum()
            sum_df_flat = final_df_flat.groupby("Date", as_index=False)["KPI"].sum()

            fig_sum = go.Figure()
            fig_sum.add_trace(go.Scatter(
                x=sum_df["Date"], y=sum_df["KPI"], mode="lines+markers", name="KPI (Difficulty)",
                line=dict(width=2.5, color="#1f77b4"), marker=dict(size=8, color="#1f77b4", line=dict(width=1.5, color="white")),
                connectgaps=True, hovertemplate="<b>📅 Date:</b> %{x}<br><b>📈 KPI (Difficulty):</b> %{y:.4f}<br><extra></extra>"
            ))
            fig_sum.add_trace(go.Scatter(
                x=sum_df_flat["Date"], y=sum_df_flat["KPI"], mode="lines+markers", name="KPI (Raw)",
                line=dict(width=2.5, color="#ff7f0e"), marker=dict(size=8, color="#ff7f0e", line=dict(width=1.5, color="white")),
                connectgaps=True, hovertemplate="<b>📅 Date:</b> %{x}<br><b>📈 KPI (Raw):</b> %{y:.4f}<br><extra></extra>"
            ))

            # The summation chart is scaled at 1.15× the base height to utilise the additional
            # vertical space available when the per-model lines are not present in this view.
            fig_sum.update_layout(
                xaxis_title="<b>Date</b>", yaxis_title="<b>Total KPI</b>", showlegend=True, margin=dict(l=85, r=20, t=50, b=110), hovermode="closest", font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1),
                legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top", bgcolor="rgba(20,20,20,0.82)", bordercolor="rgba(180,180,180,0.35)", borderwidth=1, font=dict(size=13), itemsizing="constant"),
                height=int(calculated_height * 1.15)
            )
            
            _tick_kwargs2 = dict(tickmode="auto", nticks=20) if st.session_state.time_view2 == "All Time" else dict(tickmode="linear", dtick=86400000)
            fig_sum.update_xaxes(type="date", tickformat="%b %d", tickangle=-40, automargin=True, range=[x_start2, x_end2], rangeslider=dict(visible=False), **_tick_kwargs2)
            fig_sum.update_yaxes(automargin=True, type="log" if _yscale == "Log" else "linear", rangemode="tozero" if _yscale == "From Zero" else "normal", zeroline=False)
            st.plotly_chart(fig_sum, width='stretch')