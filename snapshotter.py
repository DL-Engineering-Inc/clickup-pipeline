"""
snapshotter.py — ClickUp-to-Google Sheets Status Synchronization Engine

Performs a deterministic, idempotent synchronization of active ClickUp task records
against a Google Sheets master log.  On each run the pipeline:

  1. Streams all tasks and subtasks from the configured ClickUp list via the v2 REST
     API, applying a three-condition filter (native status, custom progression field,
     project naming convention) to isolate tasks in the First Markup / Timeline stage.
  2. Opens the target Google Sheet and maps its row structure into an in-memory block
     index keyed by immutable ClickUp task ID (column N).
  3. Executes a three-way reconciliation pass:
       UPDATE — overwrites date, duration, and task-ID cells for matched rows.
       INSERT — appends rows for tasks not yet present in the sheet.
       DELETE — removes rows whose tasks no longer satisfy the filter criteria.

Row deletions are processed in descending row-number order to prevent index-shifting
side effects.  All Google Sheets writes are wrapped in an exponential-backoff retry
loop to handle API rate limits (HTTP 429) without data loss.

Dependencies: gspread, google-auth, requests, numpy, python-dotenv
"""
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
from dotenv import load_dotenv
load_dotenv()

# --- TIMEZONE CONFIGURATION ---
EASTERN = ZoneInfo("America/New_York")

# --- DATABASE AND API CONFIGURATION ---
CLICKUP_TOKEN = os.environ.get("CLICKUP_TOKEN")
LIST_ID = "901701520995"
PROGRESSION_STATUS_FIELD_ID = "82d024f7-6f2f-4f5e-82b4-10146e18484e"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1RE039NcnPeQtQrvI5zjLyADzAr-ZseBPUq388SxkV-Y/edit"

def get_google_sheet_client():
    """
    Initializes and returns an authorized Google Spreadsheet client using service account keys.

    Returns:
        gspread.client.Client: Authenticated gspread client instance.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("creds.json", scopes=scopes)
    return gspread.authorize(creds)

def execute_with_retry(func, *args, **kwargs):
    """
    Executes a Google Sheets API call with exponential backoff to handle write-quota
    rate limits (HTTP 429).

    Retries up to 8 times with a base delay of 15 seconds, doubling on each attempt
    plus a randomised jitter to avoid thundering-herd conditions when multiple
    pipeline runs are scheduled concurrently.  Backoff schedule: 15, 30, 60, 120,
    240, 480, 960 seconds.  Non-429 exceptions are re-raised immediately.

    Args:
        func: Callable — the gspread API method to invoke.
        *args: Positional arguments forwarded to func.
        **kwargs: Keyword arguments forwarded to func.

    Returns:
        The return value of func on success.

    Raises:
        gspread.exceptions.APIError: If the error is not a rate-limit response, or
            if the maximum retry count is exhausted.
    """
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
                raise
        except Exception:
            raise

def ms_to_eastern_date(ms):
    """Convert a ClickUp UTC-millisecond timestamp to a date in Eastern time."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(EASTERN).date()

def get_clickup_tasks():
    """
    Streams all active tasks and subtasks out of ClickUp with real-time progress logging.
    Each task is tagged with a '_page' field indicating which API page it came from.

    Raises:
        RuntimeError: If any API response returns a non-200 error code, halting execution
                      to safeguard the downstream database from accidental deletion cascades.

    Returns:
        list: A consolidated list of task objects pulled from the list view.
    """
    all_tasks = []
    page = 0
    session = requests.Session()
    session.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

    print("\n[ClickUp Data Stream Engine]")
    while True:
        print(f" -> Requesting page {page} from ClickUp API...")
        url = f"https://api.clickup.com/api/v2/list/{LIST_ID}/task?subtasks=true&include_closed=false&limit=100&page={page}"

        response = None
        max_retries = 5
        base_delay = 5
        for attempt in range(max_retries):
            try:
                response = session.get(url, timeout=(10, 60))
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

        # Raises RuntimeError rather than silently continuing: an incomplete page stream would allow
        # the reconciliation phase to delete sheet rows for tasks that were simply never fetched,
        # resulting in irreversible data loss in the master log.
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            print(f" [!] ClickUp Error: Failed to fetch tasks on page {page} (Status: {status})")
            if response is not None:
                print(f"     Details: {response.text}")
            raise RuntimeError(f"API Stream Fault: ClickUp API returned error status {status} on page {page}. Sync aborted to prevent database data corruption.")

        data = response.json()
        tasks = data.get("tasks", [])

        print(f"    -> Successfully retrieved {len(tasks)} tasks from page {page}.")

        if not tasks:
            print(" -> Reached the end of the ClickUp task list.")
            break

        # Tag each task with its source page for real-time match reporting downstream
        for t in tasks:
            t["_page"] = page

        all_tasks.extend(tasks)
        page += 1

    print(f" -> Stream Complete. Total tasks pulled into memory: {len(all_tasks)}")
    return all_tasks

def main():
    """
    Entry point for the status synchronization pipeline.

    Orchestrates the full execution sequence: task streaming, filter evaluation,
    sheet structure mapping, and the three-phase reconciliation pass
    (update / insert / delete).  Aborts immediately if required credentials are
    absent or if the ClickUp API returns an incomplete response, to ensure the
    master log is never mutated with partial data.
    """
    print(f"[{datetime.now(tz=EASTERN)}] Initializing Status Synchronization Pipeline...")

    # Validate required environment variables before initiating any API calls or sheet writes.
    if not CLICKUP_TOKEN:
        print("\n[!] FATAL ERROR: ClickUp Token environment variable is missing.")
        print("    Please run the following command in your terminal before running the script:")
        print('    set CLICKUP_TOKEN="your_actual_token_here"\n')
        return

    # Fetch all qualifying tasks before opening the sheet.  A stream failure raises here,
    # before any sheet mutations, so the existing log data is never partially overwritten.
    tasks = get_clickup_tasks()

    # Keyed by immutable ClickUp Task ID (not by name, which can change mid-project)
    active_first_markup_tasks = {}

    print("\n[Evaluating Filter Conditions]")
    for task in tasks:
        task_id = task.get("id", "").strip()
        name = task.get("name", "").strip()
        if not name or not task_id:
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
            page_num = task.get("_page", "?")
            print(f" [✓] MATCH (page {page_num}): '{name}' (ID: {task_id})")

            duration_val = ""
            start_date_str = ""
            due_date_str = ""
            start_date_ms = task.get("start_date")
            due_date_ms = task.get("due_date")

            # Format and capture the ClickUp start date string (Eastern time)
            if start_date_ms:
                try:
                    start_date_str = ms_to_eastern_date(start_date_ms).strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Format and capture the ClickUp due date string (Eastern time)
            if due_date_ms:
                try:
                    due_date_str = ms_to_eastern_date(due_date_ms).strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Calculate duration in BUSINESS DAYS (Mon–Fri only, no weekends).
            if due_date_ms and start_date_ms:
                try:
                    start_date_obj = ms_to_eastern_date(start_date_ms)
                    due_date_obj   = ms_to_eastern_date(due_date_ms)
                    duration_val   = int(np.busday_count(start_date_obj, due_date_obj))
                except Exception:
                    pass

            active_first_markup_tasks[task_id] = {
                "name":       name,
                "duration":   duration_val,
                "start_date": start_date_str,
                "due_date":   due_date_str
            }

    print(f" -> Found {len(active_first_markup_tasks)} items matching [Status: TIMELINE], [Progression: FIRST MARKUP], and [Naming Code: D######].")

    # Open the master log spreadsheet and cache all row data before initiating any sheet mutations.
    print("\n[Connecting to Google Sheets]")
    client = get_google_sheet_client()
    log_sheet = client.open_by_url(GOOGLE_SHEET_URL).sheet1
    all_sheet_rows = execute_with_retry(log_sheet.get_all_values)
    print(f" -> Connection Established. Sheet data cached ({len(all_sheet_rows)} rows).")

    # Build a block index from the sheet structure.  Each parent row (non-empty column B)
    # anchors a block; column N (index 13) stores the immutable ClickUp task ID used for
    # identity resolution.  Child model rows (non-empty column C only) are collected under
    # their nearest parent block.
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
                col_n = cells[13].strip() if len(cells) > 13 else ""
                current_block = {"task_id": col_n, "name": col_b, "parent_row": row_num, "all_rows": [row_num]}
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

    # Build a name→ID reverse lookup for backward-compatible enrollment of rows that
    # predate the column N addition and therefore have no stored task ID yet.
    name_to_task_id = {info["name"]: tid for tid, info in active_first_markup_tasks.items()}

    # Reconciliation pass: compare each sheet block against live ClickUp data and apply
    # the appropriate UPDATE or REMOVE operation to bring the log into sync.
    rows_to_delete = set()
    matched_log_ids = set()
    today_str = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")

    print("\n[Synchronizing Log Table Records]")
    stored_name = ""
    p_row = 0
    try:
        for block in parent_blocks:
            stored_task_id = block["task_id"]
            stored_name    = block["name"]
            p_row          = block["parent_row"]

            # Primary match: by immutable ClickUp Task ID stored in col N
            task_id_match = stored_task_id if stored_task_id in active_first_markup_tasks else None

            # Backward-compat fallback: rows without a stored ID (pre-migration) are matched
            # by name on their first run so the ID gets written and future renames are handled.
            if not task_id_match and not stored_task_id:
                task_id_match = name_to_task_id.get(stored_name)

            if task_id_match:
                matched_log_ids.add(task_id_match)
                task_info    = active_first_markup_tasks[task_id_match]
                new_name     = task_info["name"]
                duration_str = str(task_info["duration"])
                st_date_str  = task_info["start_date"]
                due_date_str = task_info["due_date"]

                # If the task name was edited in ClickUp, update col B in place.
                # The row is NOT deleted — historical tracking continuity is preserved.
                if new_name != stored_name:
                    execute_with_retry(log_sheet.update, range_name=f"B{p_row}", values=[[new_name]], value_input_option="USER_ENTERED")
                    print(f" [✎] NAME CHANGE: Row {p_row} renamed '{stored_name}' → '{new_name}'.")

                execute_with_retry(log_sheet.update, range_name=f"A{p_row}", values=[[today_str]], value_input_option="USER_ENTERED")
                execute_with_retry(log_sheet.update, range_name=f"G{p_row}", values=[[duration_str]], value_input_option="USER_ENTERED")
                execute_with_retry(log_sheet.update, range_name=f"K{p_row}:L{p_row}", values=[[st_date_str, due_date_str]], value_input_option="USER_ENTERED")
                # Always write (or re-confirm) the task ID in col N
                execute_with_retry(log_sheet.update, range_name=f"N{p_row}", values=[[task_id_match]], value_input_option="USER_ENTERED")
                print(f" [≠] UPDATED: Synced row {p_row} for '{new_name}' (ID: {task_id_match}).")
            else:
                for r in block["all_rows"]:
                    rows_to_delete.add(r)
                print(f" [×] REMOVAL DETECTED: Task '{stored_name}' no longer matches criteria. Queued for extraction.")

    except Exception as e:
        print(f"\n[!] FATAL ERROR during sync loop at task '{stored_name}', row {p_row}: {type(e).__name__}: {e}")
        raise

    # Insertion phase: tasks present in ClickUp but not yet in the sheet are queued as new rows.
    fresh_insertions = []
    for task_id, task_info in active_first_markup_tasks.items():
        if task_id not in matched_log_ids:
            task_name    = task_info["name"]
            duration_str = str(task_info["duration"])
            st_date_str  = task_info["start_date"]
            due_date_str = task_info["due_date"]

            # 14 columns (A–N): task ID written to column N (index 13)
            fresh_insertions.append([today_str, task_name, "", "", "", "", duration_str, "", "", "", st_date_str, due_date_str, "", task_id])
            print(f" [✔] NEW RECORD DETECTED: Queued '{task_name}' (ID: {task_id}) for entry.")

    if fresh_insertions:
        print(f"\nAppending {len(fresh_insertions)} new rows to the Google Sheet tracker...")
        execute_with_retry(log_sheet.append_rows, fresh_insertions, value_input_option="USER_ENTERED")
        print(" -> Append Operations Successful.")

    # Deletion sweep: process queued row removals in descending row-number order to prevent
    # earlier deletions from shifting the indices of subsequent targets.
    if rows_to_delete:
        sorted_deletions = sorted(list(rows_to_delete), reverse=True)
        print(f"\nExecuting cleanup sweep (Wiping {len(sorted_deletions)} obsolete rows out)...")
        for target_row in sorted_deletions:
            execute_with_retry(log_sheet.delete_rows, target_row)
        print(" -> Cleanup Sweep Complete. Rows compressed cleanly.")

    print("\nSUCCESS: Progression Sync Pipeline Complete.")

if __name__ == "__main__":
    main()
