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

# Custom CSS for UI cleanup (Removed JS hacks entirely)
hide_st_style = """
            <style>
            #MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}
            div[data-testid="stToolbar"] {visibility: hidden;}
            </style>
            """
st.markdown(hide_st_style, unsafe_allow_html=True)

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
        if user_input: st.error("Invalid credentials.")
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

# --- 4. TOGGLE ENGINE & STATE MANAGEMENT ---
# Track the sidebar layout state
if "sidebar_is_open" not in st.session_state:
    st.session_state["sidebar_is_open"] = False

# Safe state persistence: prevents filters from resetting when settings are closed
def save_settings():
    if 'ui_jitter' in st.session_state: st.session_state.saved_jitter = st.session_state.ui_jitter
    if 'ui_group' in st.session_state: st.session_state.saved_group = st.session_state.ui_group
    if 'ui_projects' in st.session_state: st.session_state.saved_projects = st.session_state.ui_projects
    if 'ui_models' in st.session_state: st.session_state.saved_models = st.session_state.ui_models

# Initialize default states
if 'saved_jitter' not in st.session_state:
    st.session_state.saved_jitter = False
    st.session_state.saved_group = False
    st.session_state.saved_projects = []
    st.session_state.saved_models = []

# Conditionally render only one discreet button on the far-left indent
if not st.session_state["sidebar_is_open"]:
    if st.button("📂 Open Settings"):
        st.session_state["sidebar_is_open"] = True
        st.rerun()
else:
    if st.button("📁 Close Settings"):
        st.session_state["sidebar_is_open"] = False
        st.rerun()

# --- 5. NATIVE EMBED LAYOUT & FILTERS ---
# Emulate a sidebar natively using Streamlit Columns
if st.session_state["sidebar_is_open"]:
    settings_col, chart_col = st.columns([1, 4], gap="large")
    with settings_col:
        st.header("Graph Settings")
        st.checkbox("⚡ Separate Overlapping Points", value=st.session_state.saved_jitter, key="ui_jitter", on_change=save_settings)
        st.checkbox("📁 Group Legend by Project Code", value=st.session_state.saved_group, key="ui_group", on_change=save_settings)

        st.markdown("---")
        st.subheader("🔍 Filters")
        all_projects = sorted(df['Project Code'].unique())
        st.multiselect("Filter by Project Code:", options=all_projects, default=st.session_state.saved_projects, key="ui_projects", on_change=save_settings)
        
        filtered_df_temp = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
        all_models = sorted(filtered_df_temp['Model Name'].unique())
        st.multiselect("Filter by Specific Model:", options=all_models, default=st.session_state.saved_models, key="ui_models", on_change=save_settings)
else:
    # Give all space to the chart when closed
    chart_col = st.container()

# Apply saved filters against the dataframe
filtered_df = df if not st.session_state.saved_projects else df[df['Project Code'].isin(st.session_state.saved_projects)]
final_df = filtered_df if not st.session_state.saved_models else filtered_df[filtered_df['Model Name'].isin(st.session_state.saved_models)]

# --- 6. DATA PROCESSING & VISUALIZATION ---
view_mode = st.query_params.get("view", "all").lower()

with chart_col:
    if final_df.empty:
        st.warning("No data available for the selected filters.")
    else:
        # CHART 1: INDIVIDUAL MODELS
        if view_mode in ["all", "models"]:
            st.title("KPI per Model Over Time")
            unique_model_count = len(final_df['Model Name'].unique())
            calculated_height = max(650, (unique_model_count * 18) + 350)
            
            final_df['Display KPI'] = final_df['KPI']
            if st.session_state.saved_jitter:
                jitter_scale = max(final_df["KPI"].max() - final_df["KPI"].min(), 0.01) * 0.008
                final_df["Display KPI"] = final_df["KPI"] + final_df.groupby(["Date", "KPI"]).cumcount() * jitter_scale
                
            fig_models = px.line(final_df, x="Date", y="Display KPI", color="Model Name", markers=True, height=calculated_height, custom_data=["Model Name", "KPI", "Overlapping Models", "Overlap Count"])
            fig_models.update_traces(
                hovertemplate=("<b>🎯 Targeted Model:</b> %{customdata[0]}<br><b>📅 Date:</b> %{x}<br><b>📈 True KPI Value:</b> %{customdata[1]:.4f}<br>---------------------------------------<br><b>👥 All Models At This Coordinate (%{customdata[3]}):</b><br>%{customdata[2]}<extra></extra>"),
                hoverlabel_namelength=-1,
                line=dict(width=2.5),
                marker=dict(size=7, line=dict(width=1, color="white"))
            )
            fig_models.update_layout(xaxis_title="<b>Date</b>", yaxis_title="<b>KPI</b>", legend_title="Active Models", legend=dict(yanchor="top", y=0.99, xanchor="left", x=1.01), margin=dict(l=85, r=20, t=30, b=110), hovermode="closest", font=dict(size=13), xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), hoverlabel=dict(font_size=16, font_family="Arial", align="left"))
            
            if st.session_state.saved_group:
                for trace in fig_models.data:
                    match = re.match(r'(D\d{6})', trace.name)
                    p_code = match.group(1) if match else "Standalone"
                    trace.legendgroup = p_code
                    trace.legendgrouptitle = dict(text=f"🏢 Project {p_code}")
            
            fig_models.update_xaxes(
                type="date",
                tickformat="%b %d, %Y",
                tickangle=-40,
                tickmode="auto",
                nticks=14,
                automargin=True
            )
            fig_models.update_yaxes(automargin=True)
            st.plotly_chart(fig_models, use_container_width=True)
            if view_mode == "all": st.markdown("---")

        # CHART 2: SUMMATION
        if view_mode in ["all", "summation"]:
            st.title("KPI Summation")
            sum_df = final_df.groupby("Date", as_index=False)["KPI"].sum()
            fig_sum = px.line(sum_df, x="Date", y="KPI", markers=True, height=550)
            fig_sum.update_traces(
                line=dict(width=2.5, color="#1f77b4"),
                marker=dict(size=8, color="#1f77b4", line=dict(width=1.5, color="white"))
            )
            fig_sum.update_traces(hovertemplate=("<b>📅 Date:</b> %{x}<br><b>📈 Aggregate KPI:</b> %{y:.4f}<br><extra></extra>"))
            fig_sum.update_layout(xaxis_title="<b>Date</b>", yaxis_title="<b>Total KPI</b>", margin=dict(l=85, r=20, t=30, b=85), hovermode="closest", font=dict(size=13), xaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), yaxis_title_font=dict(size=20, family="Arial-Bold, Arial"), hoverlabel=dict(font_size=16, font_family="Arial", align="left"))
            fig_sum.update_xaxes(
                type="date",
                tickformat="%b %d, %Y",
                tickangle=-40,
                tickmode="auto",
                nticks=14,
                automargin=True
            )
            fig_sum.update_yaxes(automargin=True)
            st.plotly_chart(fig_sum, use_container_width=True)
