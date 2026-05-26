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
    /* Small top gap — just enough so the Plotly toolbar isn't clipped by the
       iframe edge. The t=50 figure margin reserves space above the plot area. */
    div[data-testid="stPlotlyChart"] { margin-top: 0.25rem; }
    /* Keep period nav buttons compact and visually tight */
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
today = datetime.date.today()

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

# Period navigation state — Chart 1 (models)
if 'time_view'      not in st.session_state: st.session_state.time_view      = "Month"
if 'period_offset'  not in st.session_state: st.session_state.period_offset  = 0

# Period navigation state — Chart 2 (summation)
if 'time_view2'     not in st.session_state: st.session_state.time_view2     = "Month"
if 'period_offset2' not in st.session_state: st.session_state.period_offset2 = 0

# Reset offset whenever the view type changes
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

def get_period_bounds(view, offset, today):
    """Return (x_start, x_end) date objects for the chosen view + offset."""
    td = datetime.timedelta
    if view == "Week":
        monday = today - td(days=today.weekday())
        start  = monday + td(weeks=offset)
        end    = start  + td(days=6)
        return start, end
    if view == "Month":
        # Shift month by offset, handling year rollovers
        raw_month = today.month - 1 + offset          # 0-based total months
        year  = today.year + raw_month // 12
        month = raw_month % 12 + 1
        start = datetime.date(year, month, 1)
        # last day of that month
        if month == 12:
            end = datetime.date(year + 1, 1, 1) - td(days=1)
        else:
            end = datetime.date(year, month + 1, 1) - td(days=1)
        return start, end
    if view == "Quarter":
        cur_q    = (today.month - 1) // 3              # 0-based quarter index
        total_q  = today.year * 4 + cur_q + offset
        q_year   = total_q // 4
        q_num    = total_q % 4                         # 0-based 0-3
        q_m_start = q_num * 3 + 1                      # 1,4,7,10
        start    = datetime.date(q_year, q_m_start, 1)
        q_m_end  = q_m_start + 2                       # last month of quarter
        if q_m_end == 12:
            end = datetime.date(q_year + 1, 1, 1) - td(days=1)
        else:
            end = datetime.date(q_year, q_m_end + 1, 1) - td(days=1)
        return start, end
    # All Time — caller handles this case
    return None, None

# Legend mode: hidden by default so chart is compact; user can expand
if 'legend_mode' not in st.session_state:
    st.session_state.legend_mode = "🚫 Hidden"

# --- Settings button only at top ---
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

# Weekdays only (Mon–Fri).
final_df = final_df[pd.to_datetime(final_df['Date']).dt.dayofweek < 5].copy()

# --- 6b. PERIOD NAVIGATION BOUNDS ---
_data_min = pd.to_datetime(final_df['Date']).min().date() if not final_df.empty else today
_data_max = pd.to_datetime(final_df['Date']).max().date() if not final_df.empty else today

_view = st.session_state.time_view
_off  = st.session_state.period_offset

if _view == "All Time":
    x_start = _data_min - datetime.timedelta(days=1)
    x_end   = _data_max + datetime.timedelta(days=1)
    _can_prev = False
    _can_next = False
else:
    x_start, x_end = get_period_bounds(_view, _off, today)
    # Prev is valid if the previous period still overlaps data
    prev_s, prev_e = get_period_bounds(_view, _off - 1, today)
    _can_prev = prev_e >= _data_min
    # Next is valid if the next period doesn't start after the furthest data point
    next_s, next_e = get_period_bounds(_view, _off + 1, today)
    _can_next = next_s <= _data_max




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

VIEW_ORDER = ["Week", "Month", "Quarter", "All Time"]

def get_nticks(view):
    return {"Week": 5, "Month": 23, "Quarter": 14, "All Time": 20}.get(view, 20)

def inline_title_nav(title, view_key, offset_key, set_fn, btn_prefix,
                     data_min, data_max, extra_col=None):
    """Render title + period buttons + Prev/Next all on one compact row.
    extra_col: optional (width, callable) for an additional widget (e.g. legend radio).
    Returns (x_start, x_end, nticks)."""
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

    with cols[ci]:
        st.markdown(f"### {title}")
    ci += 1

    if extra_col:
        with cols[ci]:
            extra_col[1]()
        ci += 1

    ci += 1  # skip spacer

    for label in VIEW_ORDER:
        with cols[ci]:
            if st.button(label, key=f"{btn_prefix}_{label}",
                         type="primary" if _v == label else "secondary"):
                set_fn(label)
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

    if _v == "All Time":
        return data_min - datetime.timedelta(days=1), data_max + datetime.timedelta(days=1), get_nticks(_v)
    xs, xe = get_period_bounds(_v, _o, today)
    return xs, xe, get_nticks(_v)


with chart_col:
    if final_df.empty:
        st.warning("No data available for the selected filters.")
    else:
        # Compute height once here so both charts can use it regardless of view_mode
        unique_model_count = len(final_df['Model Name'].unique())
        calculated_height = max(480, min(900, 300 + unique_model_count * 22))

        # ── CHART 1: INDIVIDUAL MODELS ────────────────────────────────────
        if view_mode in ["all", "models"]:

            def _legend_widget():
                st.write("")
                chosen = st.radio(
                    "Legend", options=LEGEND_OPTIONS,
                    index=LEGEND_OPTIONS.index(st.session_state.legend_mode),
                    horizontal=True, key="legend_radio_1",
                    label_visibility="collapsed",
                )
                st.session_state.legend_mode = chosen

            x_start, x_end, _nticks1 = inline_title_nav(
                "KPI per Model Over Time",
                "time_view", "period_offset", set_view, "c1",
                _data_min, _data_max,
                extra_col=(2.0, _legend_widget)
            )

            unique_model_count = len(final_df['Model Name'].unique())

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
                margin=dict(l=85, r=20, t=50, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1),
            )

            apply_legend(fig_models, st.session_state.legend_mode, inside=True)

            # One tick per calendar day for all views except All Time.
            # dtick in ms (86400000 = 1 day) forces exactly one label per date.
            _tick_kwargs1 = (
                dict(tickmode="auto", nticks=20)
                if st.session_state.time_view == "All Time"
                else dict(tickmode="linear", dtick=86400000)
            )
            fig_models.update_xaxes(
                type="date",
                tickformat="%b %d",
                tickangle=-40,
                automargin=True,
                range=[x_start, x_end],
                rangeslider=dict(visible=True, thickness=0.04, yaxis=dict(rangemode="match")),
                **_tick_kwargs1,
            )
            
            _yscale = st.session_state.saved_yscale
            fig_models.update_yaxes(
                automargin=True,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            st.plotly_chart(fig_models, width='stretch')

            if view_mode == "all":
                st.markdown("---")

        # ── CHART 2: SUMMATION ────────────────────────────────────────────
        if view_mode in ["all", "summation"]:
            x_start2, x_end2, _nticks2 = inline_title_nav(
                "KPI Summation",
                "time_view2", "period_offset2", set_view2, "c2",
                _data_min, _data_max
            )

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
                margin=dict(l=85, r=20, t=50, b=110),
                hovermode="closest",
                font=dict(size=13),
                xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"),
                hoverlabel=dict(font_size=16, font_family="Arial", align="left", namelength=-1),
            )
            
            _tick_kwargs2 = (
                dict(tickmode="auto", nticks=20)
                if st.session_state.time_view2 == "All Time"
                else dict(tickmode="linear", dtick=86400000)
            )
            fig_sum.update_xaxes(
                type="date",
                tickformat="%b %d",
                tickangle=-40,
                automargin=True,
                range=[x_start2, x_end2],
                rangeslider=dict(visible=False),
                **_tick_kwargs2,
            )
            
            _yscale = st.session_state.saved_yscale
            fig_sum.update_yaxes(
                automargin=True,
                type="log" if _yscale == "Log" else "linear",
                rangemode="tozero" if _yscale == "From Zero" else "normal",
                zeroline=False,
            )
            st.plotly_chart(fig_sum, width='stretch')
