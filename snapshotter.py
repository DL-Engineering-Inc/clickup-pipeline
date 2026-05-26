import os
import re
import time
import random
import requests
import numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials

EASTERN = ZoneInfo("America/New_York")

# --- CONFIGURATION ---
CLICKUP_TOKEN = os.environ.get("CLICKUP_TOKEN")
LIST_ID = "901701520995"  
PROGRESSION_STATUS_FIELD_ID = "82d024f7-6f2f-4f5e-82b4-10146e18484e"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1RE039NcnPeQtQrvI5zjLyADzAr-ZseBPUq388SxkV-Y/edit"

def get_google_sheet_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("creds.json", scopes=scopes)
    return gspread.authorize(creds)

def execute_with_retry(func, *args, **kwargs):
    """Protects the pipeline from Google Write Quota limitations (429 errors).
    Backoff schedule (base 15s): 15, 30, 60, 120, 240, 480, 960s — gives the
    per-minute quota window plenty of time to reset before each retry."""
    max_retries = 8
    base_delay = 15
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
                print(f" [!] Write quota limit hit (429). Retrying in {delay:.2f}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            raise e

def get_clickup_tasks():
    """Streams all active tasks and subtasks out of ClickUp with real-time progress logging."""
    all_tasks = []
    page = 0
    session = requests.Session()
    session.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
    
    print("\n[ClickUp Data Stream Engine]")
    while True:
        print(f" -> Requesting page {page} from ClickUp API...")
        url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task?subtasks=true&include_closed=false&limit=100&page={page}"

        # Fetch this page with retry + exponential backoff. ClickUp occasionally
        # responds slowly (especially with subtasks=true), and a single slow page
        # used to crash the whole run with a ReadTimeout. We now retry transient
        # network/5xx errors instead of giving up.
        # timeout=(connect, read): allow up to 60s to read the response body.
        response = None
        max_retries = 5
        base_delay = 5
        for attempt in range(max_retries):
            try:
                response = session.get(url, timeout=(10, 60))
                # Retry on transient server-side errors (rate limit / gateway issues)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    print(f" [!] ClickUp returned {response.status_code} on page {page}. Retrying in {delay:.2f}s...")
                    time.sleep(delay)
                    continue
                break
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    print(f" [!] Network error on page {page} ({type(e).__name__}). Retrying in {delay:.2f}s...")
                    time.sleep(delay)
                else:
                    print(f" [!] ClickUp Error: Page {page} failed after {max_retries} attempts ({type(e).__name__}).")
                    raise

        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            print(f" [!] ClickUp Error: Failed to fetch tasks on page {page} (Status: {status})")
            break
            
        data = response.json()
        tasks = data.get("tasks", [])
        
        print(f"    -> Successfully retrieved {len(tasks)} tasks from page {page}.")
        
        if not tasks:
            print(" -> Reached the end of the ClickUp task list.")
            break
            
        all_tasks.extend(tasks)
        page += 1
        
    print(f" -> Stream Complete. Total tasks pulled into memory: {len(all_tasks)}")
    return all_tasks

def main():
    print(f"[{datetime.now(tz=EASTERN)}] Initializing Status Synchronization Pipeline...")
    tasks = get_clickup_tasks()
    
    # 1. Parse active tasks from ClickUp matching all status and naming criteria
    active_first_markup_tasks = {}
    
    print("\n[Evaluating Filter Conditions]")
    for task in tasks:
        name = task.get("name", "").strip()
        if not name:
            continue
            
        # Condition A: Native ClickUp Status must equal TIMELINE
        native_status = str(task.get("status", {}).get("status", "")).upper().strip()
            
        # Condition B: Progression Status Custom Field must equal FIRST MARKUP
        progression_status = ""
        for field in task.get("custom_fields", []):
            if field.get("id") == PROGRESSION_STATUS_FIELD_ID:
                val = field.get("value")
                if val is not None:
                    options = field.get("type_config", {}).get("options", [])
                    if options:
                        for opt in options:
                            if str(opt.get("id")) == str(val) or str(opt.get("orderindex")) == str(val):
                                progression_status = str(opt.get("name")).upper().strip()
                                break
                    if not progression_status:
                        progression_status = str(val).upper().strip()
                        
        # Filtering Condition: Check native status, custom field status, AND the D###### project prefix
        if progression_status == "FIRST MARKUP" and native_status == "TIMELINE" and re.match(r'^D\d{6}', name):
            duration_val = ""
            start_date_str = ""
            start_date_ms = task.get("start_date")
            due_date_ms = task.get("due_date")

            def ms_to_eastern_date(ms):
                """Convert a ClickUp UTC-millisecond timestamp to a date in Eastern time."""
                return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) \
                               .astimezone(EASTERN).date()

            # Format and capture the ClickUp start date string (Eastern time)
            if start_date_ms:
                try:
                    start_date_str = ms_to_eastern_date(start_date_ms).strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Calculate duration in BUSINESS DAYS (Mon–Fri only, no weekends).
            # numpy.busday_count(start, end) counts weekdays in [start, end).
            # We add 1 to make the range inclusive of the end date, matching
            # how ClickUp displays the span.
            if due_date_ms and start_date_ms:
                try:
                    start_date_obj = ms_to_eastern_date(start_date_ms)
                    due_date_obj   = ms_to_eastern_date(due_date_ms)
                    duration_val   = int(np.busday_count(start_date_obj, due_date_obj))
                except Exception:
                    pass
                    
            active_first_markup_tasks[name] = {
                "duration": duration_val,
                "start_date": start_date_str
            }

    print(f" -> Found {len(active_first_markup_tasks)} items matching [Status: TIMELINE], [Progression: FIRST MARKUP], and [Naming Code: D######].")

    # 2. Access your Master Log spreadsheet
    print("\n[Connecting to Google Sheets]")
    client = get_google_sheet_client()
    log_sheet = client.open_by_url(GOOGLE_SHEET_URL).sheet1
    all_sheet_rows = execute_with_retry(log_sheet.get_all_values)
    print(f" -> Connection Established. Sheet data cached ({len(all_sheet_rows)} rows).")

    # 3. Structural Block Mapping Pass
    parent_blocks = []
    current_block = None
    
    try:
        for idx, cells in enumerate(all_sheet_rows):
            row_num = idx + 1
            if row_num == 1:  # Skip headers
                continue
                
            col_a = cells[0].strip() if len(cells) > 0 else ""
            col_b = cells[1].strip() if len(cells) > 1 else ""
            
            if col_b:  # Found a parent task row header
                if current_block:
                    parent_blocks.append(current_block)
                current_block = {"name": col_b, "parent_row": row_num, "all_rows": [row_num]}
            elif current_block and not col_a and not col_b:
                col_c = cells[2].strip() if len(cells) > 2 else ""
                if col_c:
                    current_block["all_rows"].append(row_num)
                    
        if current_block:
            parent_blocks.append(current_block)

    except Exception as e:
        print(f"\n[!] FATAL ERROR during sheet structure mapping at row {row_num}: {type(e).__name__}: {e}")
        raise

    print(f" -> Mapped {len(parent_blocks)} parent blocks from sheet.")

    # 4. Synchronized Evaluation & Changes Phase
    rows_to_delete = set()
    matched_log_names = set()
    today_str = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")

    print("\n[Synchronizing Log Table Records]")
    print("\n[Synchronizing Log Table Records]")
    try:
        for block in parent_blocks:
            task_name = block["name"]
            p_row = block["parent_row"]

            if task_name in active_first_markup_tasks:
                matched_log_names.add(task_name)
                task_info = active_first_markup_tasks[task_name]
                duration_str  = str(task_info["duration"])
                st_date_str   = task_info["start_date"]
                due_date_str  = task_info["due_date"]

                execute_with_retry(log_sheet.update,
                    range_name=f"A{p_row}",
                    values=[[today_str]],
                    value_input_option="USER_ENTERED")
                execute_with_retry(log_sheet.update,
                    range_name=f"G{p_row}:G{p_row}",
                    values=[[duration_str]],
                    value_input_option="USER_ENTERED")
                execute_with_retry(log_sheet.update,
                    range_name=f"K{p_row}:L{p_row}",
                    values=[[st_date_str, due_date_str]],
                    value_input_option="USER_ENTERED")
                print(f" [\u2260] UPDATED: Synced row {p_row} metrics for tracking log item '{task_name}'.")
            else:
                for r in block["all_rows"]:
                    rows_to_delete.add(r)
                print(f" [\u00d7] REMOVAL DETECTED: Task '{task_name}' no longer matches criteria. Queued for extraction.")

    except Exception as e:
        print(f"\n[!] FATAL ERROR during sync loop at task '{task_name}', row {p_row}: {type(e).__name__}: {e}")
        raise

    # 5. Append Phase for Completely New Tasks
    fresh_insertions = []
    for task_name, task_info in active_first_markup_tasks.items():
        if task_name not in matched_log_names:
            duration_str  = str(task_info["duration"])
            st_date_str   = task_info["start_date"]
            due_date_str  = task_info["due_date"]

            # Row Grid Layout:
            # A: today_str | B: task_name | G: duration_str | K: st_date_str | L: due_date_str
            fresh_insertions.append([today_str, task_name, "", "", "", "", duration_str, "", "", "", st_date_str, due_date_str])
            print(f" [✔] NEW RECORD DETECTED: Queued '{task_name}' for entry.")

    if fresh_insertions:
        print(f"\nAppending {len(fresh_insertions)} new rows to the Google Sheet tracker...")
        execute_with_retry(log_sheet.append_rows, fresh_insertions, value_input_option="USER_ENTERED")
        print(" -> Append Operations Successful.")

    # 6. Deletion Execution Sweep (Reverse Processing to prevent shifting errors)
    if rows_to_delete:
        sorted_deletions = sorted(list(rows_to_delete), reverse=True)
        print(f"\nExecuting cleanup sweep (Wiping {len(sorted_deletions)} obsolete rows out)...")
        for target_row in sorted_deletions:
            execute_with_retry(log_sheet.delete_rows, target_row)
        print(" -> Cleanup Sweep Complete. Rows compressed cleanly.")

    print("\nSUCCESS: Progression Sync Pipeline Complete.")

if __name__ == "__main__":
    main()
