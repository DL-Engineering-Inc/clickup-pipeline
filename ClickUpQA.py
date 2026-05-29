"""
ClickUpQA.py — Automated QA Subtask and Documentation Generator

For each parent task record in the Google Sheets master log that has an associated
ClickUp task ID (column N) and at least one child model entry, this script:

  1. Creates a ClickUp Doc containing a markdown QA checklist table with one row
     per constituent model (columns: Model, QA Date, Reviewer).
  2. Creates a subtask named "<task_name> - QA" under the parent ClickUp task,
     with a description that embeds a direct link to the QA Doc.
  3. Sets the Progression Status custom field on the new subtask to
     "Review Markup QA".

Idempotency: if any existing subtask under the parent already contains "QA" in
its name, that parent task is skipped, preventing duplicate QA records on
repeated runs.

The Progression Status option ID is resolved lazily from the first successfully
fetched task's custom-field metadata, avoiding an extra lookup API call.

All ClickUp and Google Sheets API calls use exponential-backoff retry wrappers
to handle transient rate-limit and server errors without manual intervention.

Dependencies: gspread, google-auth, requests, python-dotenv
"""
import os
import re
import time
import random
import requests
import gspread
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

EASTERN = ZoneInfo("America/New_York")

CLICKUP_TOKEN = os.environ.get("CLICKUP_TOKEN")
LIST_ID = "901701520995"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1RE039NcnPeQtQrvI5zjLyADzAr-ZseBPUq388SxkV-Y/edit"

PROGRESSION_STATUS_FIELD_ID = "82d024f7-6f2f-4f5e-82b4-10146e18484e"

# Matches trailing "- 1st review", "- first markup", "- 2nd Review", etc. (case-insensitive)
REVIEW_SUFFIX_RE = re.compile(
    r'\s*-\s*(?:\d+(?:st|nd|rd|th)?\s*|(?:first|second|third|fourth)\s*)?(?:review|markup)\s*$',
    re.IGNORECASE
)


def get_google_sheet_client():
    """
    Initialises and returns an authorised Google Spreadsheet client using a
    service account key file (creds.json).

    Returns:
        gspread.client.Client: Authenticated gspread client instance scoped to
        the Sheets API.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("creds.json", scopes=scopes)
    return gspread.authorize(creds)


def execute_with_retry(func, *args, **kwargs):
    """
    Executes a Google Sheets API call with exponential backoff to handle
    write-quota rate limits (HTTP 429).

    Retries up to 8 times with a base delay of 15 seconds, doubling on each
    attempt plus a randomised jitter.  Backoff schedule: 15, 30, 60, 120, 240,
    480, 960 seconds.  Non-429 errors are re-raised immediately.

    Args:
        func: Callable — the gspread API method to invoke.
        *args: Positional arguments forwarded to func.
        **kwargs: Keyword arguments forwarded to func.

    Returns:
        The return value of func on success.

    Raises:
        gspread.exceptions.APIError: If the error is not a rate-limit response,
            or if the maximum retry count is exhausted.
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


def clickup_request(session, method, url, **kwargs):
    """ClickUp API call with exponential backoff on transient 4xx/5xx errors."""
    for attempt in range(5):
        try:
            response = session.request(method, url, timeout=(10, 60), **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                delay = 5 * (2 ** attempt) + random.uniform(0, 1)
                print(f" [!] ClickUp returned {response.status_code}. Retrying in {delay:.2f}s...")
                time.sleep(delay)
                continue
            return response
        except requests.exceptions.RequestException as e:
            if attempt < 4:
                delay = 5 * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
            else:
                raise
    return None


def get_workspace_id(session):
    """Fetches the first workspace (team) ID accessible to this API token."""
    response = clickup_request(session, "GET", "https://api.clickup.com/api/v2/team")
    if response and response.status_code == 200:
        teams = response.json().get("teams", [])
        if teams:
            wid = teams[0]["id"]
            print(f" -> Workspace ID resolved: {wid}")
            return wid
    print(" [!] Could not resolve workspace ID.")
    return None


def get_task_with_subtasks(session, task_id):
    """Fetches a single ClickUp task by ID, including its subtasks array."""
    response = clickup_request(
        session, "GET",
        f"https://api.clickup.com/api/v2/task/{task_id}?include_subtasks=true",
    )
    if response and response.status_code == 200:
        return response.json()
    status = response.status_code if response else "no response"
    print(f"     [!] Failed to fetch task {task_id} (status: {status})")
    return None


def create_qa_doc(session, workspace_id, doc_name, model_names):
    """
    Creates a ClickUp Doc containing a markdown QA table page.
    Returns (doc_id, page_url) on success, or (None, None) on failure.
    URLs are constructed directly from IDs using the confirmed format:
    https://app.clickup.com/{workspace_id}/v/dc/{doc_id}/{page_id}
    """
    # Step 1: Create the Doc container
    doc_response = clickup_request(
        session, "POST",
        f"https://api.clickup.com/api/v3/workspaces/{workspace_id}/docs",
        json={"name": doc_name, "visibility": "PUBLIC", "create_page": True},
    )
    if doc_response is None or doc_response.status_code not in (200, 201):
        status = doc_response.status_code if doc_response else "no response"
        body = doc_response.text[:300] if doc_response else ""
        print(f"     [!] Doc creation failed (status {status}): {body}")
        return None, None

    doc_id = doc_response.json().get("id")
    fallback_url = f"https://app.clickup.com/{workspace_id}/v/dc/{doc_id}" if doc_id else ""

    # Step 2: Add a page with the markdown table
    table_lines = ["| Model | QA Date | By (Use @) |", "|---|---|---|"]
    for name in model_names:
        table_lines.append(f"| {name} |  |  |")

    page_response = clickup_request(
        session, "POST",
        f"https://api.clickup.com/api/v3/workspaces/{workspace_id}/docs/{doc_id}/pages",
        json={
            "name": "QA Checklist",
            "content_format": "text/md",
            "content": "\n".join(table_lines),
        },
    )
    if page_response and page_response.status_code in (200, 201):
        page_id = page_response.json().get("id")
        page_url = (
            f"https://app.clickup.com/{workspace_id}/v/dc/{doc_id}/{page_id}"
            if doc_id and page_id else fallback_url
        )
        return doc_id, page_url

    status = page_response.status_code if page_response else "no response"
    print(f"     [!] Page creation failed (status {status}) — using doc-level URL.")
    return doc_id, fallback_url


def find_field_option_id(task, field_id, option_name):
    """
    Finds the option ID for a named dropdown custom field value from a single task object.
    type_config.options contains all available options for the field regardless of whether
    the task itself has that value selected.
    """
    target = option_name.strip().upper()
    for field in task.get("custom_fields", []):
        if field.get("id") == field_id:
            for opt in field.get("type_config", {}).get("options", []):
                if opt.get("name", "").strip().upper() == target:
                    return opt.get("id")
    return None


def strip_review_suffix(task_name):
    """Removes trailing review/markup suffix from a task name."""
    return REVIEW_SUFFIX_RE.sub("", task_name).strip()


def main():
    print("[ClickUp QA Subtask Creator]")

    # 1. Read Google Sheet — parse parent task blocks, model children, and task IDs (col N)
    print("\n[Reading Google Sheet Project Progress Log]")
    client = get_google_sheet_client()
    log_sheet = client.open_by_url(GOOGLE_SHEET_URL).sheet1
    all_rows = execute_with_retry(log_sheet.get_all_values)
    print(f" -> Read {len(all_rows)} rows.")

    parent_blocks = []
    current_block = None
    for idx, cells in enumerate(all_rows):
        if idx == 0:  # skip header row
            continue
        col_a = cells[0].strip() if len(cells) > 0 else ""
        col_b = cells[1].strip() if len(cells) > 1 else ""

        if col_b:  # parent task row — col N (index 13) holds the ClickUp task ID
            if current_block:
                parent_blocks.append(current_block)
            col_n = cells[13].strip() if len(cells) > 13 else ""
            current_block = {"name": col_b, "task_id": col_n, "models": []}
        elif current_block and not col_a and not col_b:  # child model row
            col_c = cells[2].strip() if len(cells) > 2 else ""
            if col_c:
                current_block["models"].append(col_c)

    if current_block:
        parent_blocks.append(current_block)
    print(f" -> Parsed {len(parent_blocks)} parent task blocks.")

    # 2. Set up session and resolve workspace ID (needed for Doc API v3)
    session = requests.Session()
    session.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

    print("\n[Resolving Workspace]")
    workspace_id = get_workspace_id(session)
    if not workspace_id:
        print(" [!] FATAL: Cannot proceed without a workspace ID.")
        return

    # 3. Process each sheet parent task
    print(f"\n[Processing {len(parent_blocks)} sheet tasks]")
    created = 0
    skipped_no_id = 0
    skipped_no_models = 0
    skipped_fetch_fail = 0
    skipped_qa_exists = 0

    # Resolved lazily from the first successfully fetched task's field metadata
    qa_option_id = None

    for block in parent_blocks:
        sheet_name = block["name"]
        task_id = block["task_id"]
        models = block["models"]

        # No task ID stored in the sheet — cannot proceed for this row
        if not task_id:
            print(f" [~] No task ID in sheet for '{sheet_name}' — skipping.")
            skipped_no_id += 1
            continue

        # No child models listed — nothing meaningful to QA
        if not models:
            print(f" [-] No models under '{sheet_name}' — skipping.")
            skipped_no_models += 1
            continue

        # Fetch the task directly by ID (includes subtasks array and custom field options)
        task_data = get_task_with_subtasks(session, task_id)
        if not task_data:
            skipped_fetch_fail += 1
            continue

        # Lazily resolve the "Review Markup QA" option ID from the first fetched task
        if qa_option_id is None:
            qa_option_id = find_field_option_id(task_data, PROGRESSION_STATUS_FIELD_ID, "Review Markup QA")
            if qa_option_id:
                print(f" -> 'Review Markup QA' option ID: {qa_option_id}")
            else:
                print(" [!] Warning: Could not find 'Review Markup QA' option — progression status will not be set.")

        # Skip if any existing subtask already contains "QA"
        existing_subtask_names = [st.get("name", "") for st in task_data.get("subtasks", [])]
        if any("QA" in s.upper() for s in existing_subtask_names):
            print(f" [=] QA subtask already exists under '{sheet_name}' — skipping.")
            skipped_qa_exists += 1
            continue

        subtask_name = f"{strip_review_suffix(sheet_name)} - QA"
        print(f" [+] Creating '{subtask_name}' under '{sheet_name}'...")

        # 3a. Create the QA Doc with the model table
        doc_id, doc_page_url = create_qa_doc(
            session, workspace_id,
            f"{subtask_name} — QA Table",
            models,
        )

        md_description = (
            "QA tracking table for this task is maintained in a structured ClickUp document.\n\n"
            f"👉 **View QA Table:** [Open QA Document]({doc_page_url})"
        )

        # 3b. Create the subtask — status Timeline, doc link in description
        create_response = clickup_request(
            session, "POST",
            f"https://api.clickup.com/api/v2/list/{LIST_ID}/task",
            json={
                "name": subtask_name,
                "markdown_description": md_description,
                "parent": task_id,
                "status": "Timeline",
            },
        )

        if create_response is None or create_response.status_code not in (200, 201):
            status = create_response.status_code if create_response is not None else "no response"
            body = create_response.text[:300] if create_response is not None else ""
            print(f"     -> Subtask creation failed (status {status}): {body}")
            continue

        subtask_id = create_response.json().get("id", "?")
        print(f"     -> Subtask created. ID: {subtask_id}")

        # 3c. Set the Progression Status custom field to "Review Markup QA"
        if qa_option_id and subtask_id != "?":
            field_response = clickup_request(
                session, "POST",
                f"https://api.clickup.com/api/v2/task/{subtask_id}/field/{PROGRESSION_STATUS_FIELD_ID}",
                json={"value": qa_option_id},
            )
            if field_response and field_response.status_code in (200, 201):
                print(f"     -> Progression status set to 'Review Markup QA'.")
            else:
                fs = field_response.status_code if field_response else "no response"
                print(f"     [!] Failed to set progression status (status {fs}).")

        created += 1

    print(
        f"\n[Done] Created: {created} | "
        f"Skipped (no task ID): {skipped_no_id} | "
        f"Skipped (no models): {skipped_no_models} | "
        f"Skipped (fetch failed): {skipped_fetch_fail} | "
        f"Skipped (QA exists): {skipped_qa_exists}"
    )


if __name__ == "__main__":
    main()
