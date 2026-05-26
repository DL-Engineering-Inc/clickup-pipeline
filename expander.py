import re
import io
import time
import random
import numpy as np
import pandas as pd
import openpyxl
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- CONFIGURATION ---
GOOGLE_SHEET_URL  = "https://docs.google.com/spreadsheets/d/1RE039NcnPeQtQrvI5zjLyADzAr-ZseBPUq388SxkV-Y/edit?usp=sharing"
KPI_INDEX_SHEET_URL = "https://docs.google.com/spreadsheets/d/1xJwiD1F_p6BFm4-sjEEZ0U1xCMqqUEU6TwQSUmQxW5s/edit"
DRIVE_FOLDER_ID   = "1zHACpi08NE9D9tg5HTb_jbkjV6RpKI2v"

def get_google_credentials():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    return Credentials.from_service_account_file("creds.json", scopes=scopes)

def execute_with_retry(func, *args, **kwargs):
    """Automatic exponential backoff handler for Google API rate limits."""
    max_retries = 5
    base_delay = 5
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f" [!] Rate limit hit (429). Server busy. Sleeping for {delay:.2f}s before retry...")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            raise e

def list_spreadsheets_in_folder(folder_id, service):
    """Scans the designated Google Drive folder and maps file names to their unique file IDs."""
    files_map = {}
    page_token = None
    
    print(f" -> Accessing Google Drive Folder: {folder_id}...")
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        response = service.files().list(
            q=query,
            spaces='drive',
            fields='nextPageToken, files(id, name)',
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        
        for file in response.get('files', []):
            files_map[file['name'].upper()] = file['id']
            
        page_token = response.get('nextPageToken', None)
        if not page_token:
            break
            
    print(f" -> Found {len(files_map)} schedule files inside the folder.")
    return files_map

def parse_number_sequence(seg):
    """Helper to convert sequences like '1 to 8', '1-8', or '9, 10' into lists of integers."""
    seg = seg.upper()
    numbers = set()
    ranges = re.findall(r'(\d+)\s*(?:TO|-)\s*(\d+)', seg)
    for start, end in ranges:
        for i in range(int(start), int(end) + 1):
            numbers.add(i)
        seg = re.sub(rf'{start}\s*(?:TO|-)\s*{end}', '', seg)
    extra_nums = re.findall(r'\d+', seg)
    for n in extra_nums:
        numbers.add(int(n))
    return sorted(list(numbers))

def parse_compound_suffixes(blob):
    """Parses complex trailing model modifiers like '8 TO 10, 13E, 13EU, 14C, 15'."""
    blob = blob.upper()
    suffixes = []
    range_match = re.search(r'(\d+)\s*(?:TO|-)\s*(\d+)', blob)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        for i in range(start, end + 1):
            suffixes.append(str(i))
        blob = re.sub(r'\d+\s*(?:TO|-)\s*\d+', '', blob)
    parts = [p.strip() for p in blob.split(',') if p.strip()]
    for p in parts:
        if p:
            suffixes.append(p)
    return suffixes

def is_wildcard_numeric_match(allowed_model, norm_model_cell):
    """Handles number wildcards like '6000S' or '36S' by searching for matching sequences."""
    if allowed_model.endswith('S'):
        base = allowed_model[:-1]
        if base.isdigit():
            if base in norm_model_cell:
                return True
            stripped = base.rstrip('0')
            if stripped and norm_model_cell.startswith(stripped):
                return True
    return False

def extract_allowed_targets(task_name):
    """Advanced multi-token tokenization engine processing Models, Blocks, and Lots."""
    project_match = re.search(r'(D\d{5,6})', task_name)
    if not project_match:
        return None, set(), set(), set()
    project_code = project_match.group(1)
    
    text = task_name.upper().replace(project_code.upper(), "")
    text = re.split(r'\b\d+(?:ST|ND|RD|TH)\s+REVIEW', text)[0].strip()
    
    allowed_models = set()
    allowed_blocks = set()
    allowed_lots = set()
    
    block_segments = re.findall(r'\b(?:BLOCK|BLK\.?)\s*([\d\s,ToTO\-]+)', text)
    for seg in block_segments:
        nums = parse_number_sequence(seg)
        for num in nums:
            allowed_blocks.add(f"BLOCK {num}")
            
    text_no_blocks = re.sub(r'\b(?:BLOCK|BLK\.?)\s*[\d\s,ToTO\-]+', '', text)
    
    lot_segments = re.findall(r'\b(?:LOT|LTS|LOTS)\s*([\d\s,ToTO\-]+)', text_no_blocks)
    for seg in lot_segments:
        nums = parse_number_sequence(seg)
        for num in nums:
            allowed_lots.add(num)

    def add_model_target(series, suffix):
        series = series.upper().strip()
        suffix = suffix.upper().strip()
        if suffix:
            allowed_models.add(f"{series}{suffix}")
            allowed_models.add(f"{series}-{suffix}")
            allowed_models.add(f"{series} {suffix}")
        else:
            allowed_models.add(series)
        
        if len(series) > 1 and series.endswith('S'):
            base_series = series[:-1]
            if suffix:
                allowed_models.add(f"{base_series}{suffix}")
                allowed_models.add(f"{base_series}-{suffix}")
                allowed_models.add(f"{base_series} {suffix}")
            else:
                allowed_models.add(base_series)

    range_matches = re.findall(r'\b([A-Z-]{1,4})\s*-?\s*(\d+)\s*(?:TO|-)\s*(?:[A-Z-]{1,4}\s*-?\s*)?(\d+)\b', text_no_blocks)
    for series, start_str, end_str in range_matches:
        start, end = int(start_str), int(end_str)
        padding = len(start_str)
        for i in range(start, end + 1):
            num_str = f"{i:0{padding}d}"
            add_model_target(series, num_str)
            if start_str.startswith('0'):
                add_model_target(series, str(i))

    # Handle ranges where the series prefix is itself numeric, e.g.:
    #   "36-01 TO 04"       → 36-01, 36-02, 36-03, 36-04
    #   "36-01 TO 36-04"    → 36-01, 36-02, 36-03, 36-04
    #   "40-01 TO 04"       → 40-01, 40-02, 40-03, 40-04
    # Pattern: BASE(2-4 digits) - STARTNUM  TO  [BASE-]? ENDNUM
    # We require at least 2 digits in each part so single-digit tokens
    # (which the letter-series path already handles) are not double-processed.
    numeric_prefix_ranges = re.findall(
        r'\b(\d{2,4})-(\d{2,4})\s+TO\s+(?:\d{2,4}-)?(\d{2,4})\b',
        text_no_blocks
    )
    for base, start_str, end_str in numeric_prefix_ranges:
        start, end = int(start_str), int(end_str)
        # If end < start the author likely meant the suffix only (e.g. "36-01 TO 04"
        # where 04 < 36). That is the normal case — expand suffix from start to end.
        # Guard against obviously bad ranges (end much larger than start with no
        # shared prefix context) by capping at 200 models per range.
        if start <= end and (end - start) <= 200:
            padding = len(start_str)
            for i in range(start, end + 1):
                num_str = f"{i:0{padding}d}"
                add_model_target(base, num_str)

    pure_num_ranges = re.findall(r'\b(\d+)\s*(?:TO|-)\s*(\d+)\b', text_no_blocks)
    for start_str, end_str in pure_num_ranges:
        start, end = int(start_str), int(end_str)
        padding = len(start_str)
        for i in range(start, end + 1):
            allowed_models.add(f"{i:0{padding}d}")

    compound_match = re.search(r'\b([A-Z-]{1,4})\s*[-]?\s*(\d+)\s*-\s*([^-\n]+)', text_no_blocks)
    if compound_match:
        prefix_series = compound_match.group(1)
        base_num = compound_match.group(2)
        suffixes = parse_compound_suffixes(compound_match.group(3))
        for suf in suffixes:
            add_model_target(prefix_series, f"{base_num}-{suf}")

    p23 = re.search(r'\b([A-Z-]{1,4})\s*\(\s*(\d+)\s*(?:TO|-)\s*(\d+)\s*\)', text_no_blocks)
    if p23:
        series = p23.group(1)
        for i in range(int(p23.group(2)), int(p23.group(3)) + 1):
            add_model_target(series, str(i))

    p1 = re.search(r'\b([A-Z-]{1,4})\s*(\d+(?:\s*,\s*\d+)+)', text_no_blocks)
    if p1:
        series = p1.group(1)
        for n in p1.group(2).split(','):
            add_model_target(series, n.strip())

    all_tokens = re.findall(r'\b([A-Z0-9-]+)\b', text_no_blocks)
    for token in all_tokens:
        allowed_models.add(token)
        allowed_models.add(token.replace('-', ''))
        if token.endswith('S') and len(token) > 1:
            allowed_models.add(token[:-1])
            allowed_models.add(token[:-1].replace('-', ''))

    return project_code, allowed_models, allowed_blocks, allowed_lots

def parse_elevation_count(elevation_cell):
    """Calculates clean integer elevation profiles, correcting un-slashed typos."""
    if not elevation_cell or str(elevation_cell).strip() in ["-", ""]:
        return 1
    el_str = str(elevation_cell).strip().upper()
    
    if any(char in el_str for char in ['/', ',', '+', '&']):
        elements = re.split(r'[/,&+]', el_str)
        return len([e for e in elements if e.strip()])
        
    if el_str.isalpha():
        return len(el_str)
        
    return 1

def load_difficulty_brackets(all_rows):
    """
    Reads the sq ft → difficulty bracket table from rows 2–4 of the KPI Index sheet.
    Expected format:
        Row 2:  "0-2500"    | 1
        Row 3:  "2501-4000" | 1.5
        Row 4:  "4001+"     | 2
    Returns a list of (lo, hi, difficulty) tuples sorted by lo.
    """
    brackets = []
    for row in all_rows[1:5]:   # sheet rows 2-4 (0-indexed 1-3), row 5 is blank
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        range_str = row[0].strip().replace(',', '')
        try:
            diff_val = float(row[1].strip())
        except ValueError:
            continue
        if '+' in range_str:
            num_part = re.sub(r'[^0-9]', '', range_str)
            if num_part:
                brackets.append((float(num_part), float('inf'), diff_val))
        elif '-' in range_str:
            parts = range_str.split('-')
            try:
                brackets.append((float(parts[0].strip()), float(parts[1].strip()), diff_val))
            except (ValueError, IndexError):
                continue
    brackets.sort(key=lambda x: x[0])
    return brackets


def calc_base_difficulty(sq_ft, brackets):
    """Return the base difficulty for sq_ft using the loaded bracket table."""
    for lo, hi, diff in brackets:
        if lo <= sq_ft <= hi:
            return diff
    return brackets[-1][2] if brackets else 1.0


def parse_sq_ft_and_difficulty(sq_ft_val, num_elevations, difficulty_brackets=None):
    """Sanitizes square footage and assigns structural difficulty brackets."""
    s = str(sq_ft_val).upper().replace(",", "").strip()
    s_match = re.search(r'([\d/]+)', s)
    if not s_match:
        return "0", 1.0
        
    val_part = s_match.group(1)
    if "/" in val_part:
        parts = [float(x.strip()) for x in val_part.split("/") if x.strip()]
        sq_ft = max(parts) if parts else 0
    else:
        sq_ft = float(val_part)

    # Use dynamic brackets from KPI Index sheet when available; fall back to hardcoded.
    if difficulty_brackets:
        base_difficulty = calc_base_difficulty(sq_ft, difficulty_brackets)
    else:
        if sq_ft <= 2500:
            base_difficulty = 1.0
        elif sq_ft <= 4000:
            base_difficulty = 1.5
        else:
            base_difficulty = 2.0
        
    elevation_bonus = max(0, (num_elevations - 1) * 0.1)
    difficulty = round(base_difficulty + elevation_bonus, 2)
    
    sq_ft_str = str(sq_ft)
    if sq_ft_str.endswith(".0"):
        sq_ft_str = sq_ft_str[:-2]
        
    return sq_ft_str, difficulty

def find_date_column_indices(schedule_data):
    """Scans top rows of the workbook to locate date column offsets."""
    arch_idx, floor_idx, truss_idx = None, None, None
    for row in schedule_data[:5]:
        row_upper = [str(cell).upper().strip() for cell in row]
        if any("ARCH DWG RECEIVED" in c or "MODEL" in c for c in row_upper):
            for idx, text in enumerate(row_upper):
                if "ARCH DWG RECEIVED" in text:
                    arch_idx = idx
                elif "FLOOR LAYOUT RECEIVED" in text or "FLOOR RECEIVED" in text:
                    floor_idx = idx
                elif "TRUSS LAYOUT RECEIVED" in text or "TRUSS RECEIVED" in text:
                    truss_idx = idx
            break
    return arch_idx, floor_idx, truss_idx

def clean_and_parse_date(date_str):
    """Helper to cleanly parse Excel date strings into explicit date objects."""
    if not date_str or date_str.strip() in ["", "NAN", "-", "NONE"]:
        return None
    date_clean = date_str.split(" ")[0].strip()
    date_clean = date_clean.replace("/", "-")
    if re.match(r'^\d{4}-\d{2}-\d{2}', date_clean):
        try:
            return datetime.strptime(date_clean[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None

def main():
    print(f"[{datetime.now()}] Initializing Advanced Inline Upsert Engine with Balanced Loading metrics...")
    creds = get_google_credentials()
    client = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    
    try:
        drive_files = list_spreadsheets_in_folder(DRIVE_FOLDER_ID, drive_service)
        master_workbook = client.open_by_url(GOOGLE_SHEET_URL)
        log_sheet = master_workbook.sheet1

        # --- KPI DIFFICULTY INDEX SHEET SETUP ---
        print("\n[Loading KPI Difficulty Index Sheet]")
        kpi_index_wb    = client.open_by_url(KPI_INDEX_SHEET_URL)
        kpi_index_sheet = kpi_index_wb.sheet1
        kpi_index_all   = execute_with_retry(kpi_index_sheet.get_all_values)

        # Load dynamic sq ft → difficulty bracket table from rows 2-4
        difficulty_brackets = load_difficulty_brackets(kpi_index_all)
        print(f" -> Loaded {len(difficulty_brackets)} difficulty brackets.")

        all_log_rows = log_sheet.get_all_values()
        
        print("\nScanning rows and executing architectural upsert calculations...")
        for idx in range(len(all_log_rows) - 1, 0, -1):
            row = all_log_rows[idx]
            if len(row) < 2 or not row[1].strip():
                continue
                
            task_name = row[1]  
            project_code, allowed_models, allowed_blocks, allowed_lots = extract_allowed_targets(task_name)
            
            if not project_code or (not allowed_models and not allowed_blocks and not allowed_lots):
                continue
                
            matched_file_id = None
            for filename, file_id in drive_files.items():
                if project_code in filename:
                    matched_file_id = file_id
                    break
                    
            if not matched_file_id:
                continue
                
            # --- DEDUPLICATION SCAN PHASE ---
            existing_children = {}  
            scan_idx = idx + 1
            while scan_idx < len(all_log_rows):
                scan_row = all_log_rows[scan_idx]
                if (len(scan_row) > 0 and scan_row[0].strip()) or (len(scan_row) > 1 and scan_row[1].strip()):
                    break
                if len(scan_row) > 2 and scan_row[2].strip():
                    raw_model_name = scan_row[2].strip().upper()
                    cleaned_key = re.sub(r'[^A-Z0-9]', '', raw_model_name)
                    existing_children[cleaned_key] = scan_idx + 1
                scan_idx += 1
                
            try:
                file_bytes = drive_service.files().get_media(fileId=matched_file_id).execute()
                
                wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
                ws = wb.worksheets[0]
                
                for merged_range in list(ws.merged_cells.ranges):
                    min_row, max_row = merged_range.min_row, merged_range.max_row
                    min_col, max_col = merged_range.min_col, merged_range.max_col
                    
                    top_left_value = ws.cell(row=min_row, column=min_col).value
                    ws.unmerge_cells(start_row=min_row, start_column=min_col, end_row=max_row, end_column=max_col)
                    
                    for r in range(min_row, max_row + 1):
                        for c in range(min_col, max_col + 1):
                            ws.cell(row=r, column=c).value = top_left_value
                
                schedule_data = []
                for sheet_row in ws.iter_rows(values_only=True):
                    clean_row = [str(cell).strip() if cell is not None else "" for cell in sheet_row]
                    schedule_data.append(clean_row)
                
                arch_idx, floor_idx, truss_idx = find_date_column_indices(schedule_data)
                
                subtask_compiled_matches = []
                current_series = ""
                
                for s_row in schedule_data:
                    if len(s_row) < 5:
                        continue
                        
                    series_cell = s_row[1].strip()
                    model_cell = s_row[2].strip()
                    
                    if model_cell.endswith(".0"):
                        model_cell = model_cell[:-2]
                        
                    if series_cell:
                        current_series = series_cell
                        
                    if not model_cell:
                        continue
                        
                    model_upper = model_cell.upper()
                    norm_model_cell = re.sub(r'[^A-Z0-9]', '', model_upper)
                    
                    is_match = False
                    is_block = False
                    matched_via_elevation_suffix = False
                    suffix_elevation_count = 1
                    matched_am_token = ""
                    
                    for am in allowed_models:
                        am_upper = am.upper()
                        match_suff = re.search(r'^(.+?)(([A-Z])+)$', am_upper)
                        if match_suff:
                            base_am = match_suff.group(1)
                            suff_str = match_suff.group(2)
                            if norm_model_cell == re.sub(r'[^A-Z0-9]', '', base_am):
                                if all(c in s_row[3].upper() for c in suff_str):
                                    is_match = True
                                    matched_via_elevation_suffix = True
                                    suffix_elevation_count = len(suff_str)
                                    matched_am_token = am
                                    break
                                    
                        if norm_model_cell == re.sub(r'[^A-Z0-9]', '', am_upper):
                            is_match = True
                            break
                        if is_wildcard_numeric_match(am_upper, norm_model_cell):
                            is_match = True
                            break
                                    
                    if not is_match:
                        for ab in allowed_blocks:
                            if ab in model_upper or re.sub(r'[^A-Z0-9]', '', ab) in norm_model_cell:
                                is_match = True
                                is_block = True
                                break
                                
                    if not is_match:
                        for al_num in allowed_lots:
                            if f"LOT {al_num}" in model_upper or f"LOT0{al_num}" in model_upper or f"LOT {al_num})" in model_upper:
                                is_match = True
                                break
                                
                    if is_match:
                        if matched_via_elevation_suffix:
                            model_output = matched_am_token
                            num_elevations = suffix_elevation_count
                        else:
                            model_output = model_cell
                            num_elevations = parse_elevation_count(s_row[3])
                        
                        if is_block:
                            sq_ft_output = ""
                            difficulty_output = "1"
                        else:
                            sq_ft_output, difficulty_output = parse_sq_ft_and_difficulty(s_row[4], num_elevations, difficulty_brackets)
                            
                        arch_cell_str = s_row[arch_idx].strip() if arch_idx is not None and arch_idx < len(s_row) else ""
                        floor_cell_str = s_row[floor_idx].strip() if floor_idx is not None and floor_idx < len(s_row) else ""
                        truss_cell_str = s_row[truss_idx].strip() if truss_idx is not None and truss_idx < len(s_row) else ""
                        
                        arch_dt = clean_and_parse_date(arch_cell_str)
                        floor_dt = clean_and_parse_date(floor_cell_str)
                        truss_dt = clean_and_parse_date(truss_cell_str)
                        
                        subtask_compiled_matches.append({
                            "model_output": model_output,
                            "is_block": is_block,
                            "num_elevations": num_elevations,
                            "sq_ft_output": sq_ft_output,
                            "difficulty_output": difficulty_output,
                            "arch_dt": arch_dt,
                            "floor_dt": floor_dt,
                            "truss_dt": truss_dt,
                            "final_start_date": ""
                        })

                # Step 2: Advanced Two-Pass Relational Scheduling Calculations
                valid_model_dates = []
                for item in subtask_compiled_matches:
                    if not item["is_block"]:
                        if item["arch_dt"] and item["floor_dt"] and item["truss_dt"]:
                            calc_date = max(item["arch_dt"], item["floor_dt"], item["truss_dt"]) + timedelta(days=1)
                            item["final_start_date"] = calc_date.strftime("%Y-%m-%d")
                            valid_model_dates.append(calc_date)
                        else:
                            item["final_start_date"] = ""  

                max_subtask_model_date = max(valid_model_dates) if valid_model_dates else None

                for item in subtask_compiled_matches:
                    if item["is_block"]:
                        if item["arch_dt"] and item["truss_dt"]:
                            block_base_date = max(item["arch_dt"], item["truss_dt"]) + timedelta(days=1)
                            if max_subtask_model_date and max_subtask_model_date > block_base_date:
                                final_block_date = max_subtask_model_date
                            else:
                                final_block_date = block_base_date
                            item["final_start_date"] = final_block_date.strftime("%Y-%m-%d")
                        else:
                            item["final_start_date"] = ""

                # Step 3: Upsert Execution Phase
                new_rows_to_insert = []
                project_sheet_updates = []
                seen_models_this_pass = set()
                
                for item in subtask_compiled_matches:
                    model_output      = item["model_output"]
                    num_elevations    = item["num_elevations"]
                    sq_ft_output      = item["sq_ft_output"]
                    difficulty_output = item["difficulty_output"]
                    start_date_output = item["final_start_date"]

                    # Calculate Loading Quotient → Difficulty / Parent Duration (Col G)
                    try:
                        parent_col_g = float(row[6].strip()) if len(row) > 6 and row[6].strip() else 0
                        model_difficulty = float(difficulty_output) if difficulty_output else 0
                        if parent_col_g != 0 and model_difficulty != 0:
                            loading_quotient = round(model_difficulty / parent_col_g, 4)
                        else:
                            loading_quotient = ""
                    except Exception:
                        loading_quotient = ""

                    # Target unique tracking code from Parent Task Row Column K
                    parent_col_k_str = row[10].strip() if len(row) > 10 else ""

                    # Deduplicate array staging to eliminate dual updates
                    if model_output.upper() not in seen_models_this_pass:
                        seen_models_this_pass.add(model_output.upper())
                        project_sheet_updates.append({
                            "model_name": f"{project_code} - {model_output}",
                            "loading_quotient": loading_quotient,
                            "clickup_start_date": parent_col_k_str,
                            # Duration lives only on the parent task row (Sheet 1 Col G) and
                            # is assumed identical for every model/block beneath it.
                            "duration": row[6].strip() if len(row) > 6 else "",
                            # Due date from Sheet 1 Col L — used to define the exact last
                            # business day of the projection window in Sheet 2.
                            "clickup_due_date": row[11].strip() if len(row) > 11 else ""
                        })
                    
                    model_lookup_key = re.sub(r'[^A-Z0-9]', '', model_output.upper())
                    if model_lookup_key in existing_children:
                        live_row_num = existing_children[model_lookup_key]
                        orig_child_row = all_log_rows[live_row_num - 1]
                        
                        # Track cells directly across columns D through K safely
                        current_el = orig_child_row[3].strip() if len(orig_child_row) > 3 else ""
                        current_sq = orig_child_row[4].strip() if len(orig_child_row) > 4 else ""
                        current_diff = orig_child_row[5].strip() if len(orig_child_row) > 5 else ""
                        current_g = orig_child_row[6].strip() if len(orig_child_row) > 6 else ""     # Col G
                        current_h = orig_child_row[7].strip() if len(orig_child_row) > 7 else ""     # Col H
                        current_i = orig_child_row[8].strip() if len(orig_child_row) > 8 else ""     # Col I
                        current_j = orig_child_row[9].strip() if len(orig_child_row) > 9 else ""     # Col J
                        current_k = orig_child_row[10].strip() if len(orig_child_row) > 10 else ""   # Col K
                        
                        new_el_str = str(num_elevations)
                        new_sq_str = str(sq_ft_output)
                        new_diff_str = str(difficulty_output)
                        new_g_str = ""
                        new_h_str = str(loading_quotient)
                        new_i_str = ""
                        new_j_str = str(start_date_output)
                        new_k_str = str(parent_col_k_str)
                        
                        # Bulk range push execution if metrics differ
                        if (current_el != new_el_str or current_sq != new_sq_str or 
                            current_diff != new_diff_str or current_g != new_g_str or 
                            current_h != new_h_str or current_i != new_i_str or 
                            current_j != new_j_str or current_k != new_k_str):
                            
                            update_range = f"D{live_row_num}:K{live_row_num}"
                            update_payload = [[new_el_str, new_sq_str, new_diff_str, new_g_str, new_h_str, new_i_str, new_j_str, new_k_str]]
                            
                            execute_with_retry(
                                log_sheet.update,
                                range_name=update_range,
                                values=update_payload,
                                value_input_option="USER_ENTERED"
                            )
                            print(f" [≠] UPSERT RANGE SYNC: Row {live_row_num} matrix synchronized for item '{model_output}'.")
                    else:
                        # NEW GRID STRUCTURAL MAP:
                        # A: Blank | B: Blank | C: Name | D: Elevations | E: Sq Ft | F: Difficulty 
                        # G: Unused/Blank | H: Quotient | I: Unused/Blank | J: Theoretical Start Date | K: Parent Task Col K
                        new_rows_to_insert.append([
                            "",                             
                            "",                             
                            model_output,                     
                            num_elevations,                 
                            sq_ft_output,                   
                            difficulty_output,              
                            "",                             # Column G (Unused for Child Records)
                            loading_quotient,               # Column H (Difficulty / Parent Column G)
                            "",                             # Column I (Unused for Child Records)
                            start_date_output,              # Column J (Theoretical Start Date)
                            parent_col_k_str                # Column K (Inherited from Parent Task Row Column K)
                        ])
                
                if new_rows_to_insert:
                    insertion_line = idx + 2
                    execute_with_retry(
                        log_sheet.insert_rows,
                        new_rows_to_insert,
                        row=insertion_line,
                        value_input_option="USER_ENTERED"
                    )
                    print(f" [✔] INJECTED: Added {len(new_rows_to_insert)} balanced matrix rows under row {idx + 1}")
                
                # --- UPGRADE FEATURE: CONSOLIDATED SINGLE-TAB LOG WRITER ---
                if project_sheet_updates:
                    try:
                        # UPGRADE FIX: Targets the second tab sheet index [1] directly instead of project titles
                        proj_sheet = master_workbook.worksheets()[1]
                    except IndexError:
                        # Safeguard fall-back if sheet 2 does not exist
                        proj_sheet = execute_with_retry(
                            master_workbook.add_worksheet,
                            title="Consolidated Model Log",
                            rows="2000",
                            cols="5"
                        )
                        execute_with_retry(
                            proj_sheet.append_row,
                            ["Model Name", "Date", "Loading Quotient"],
                            value_input_option="USER_ENTERED"
                        )
                    
                    proj_rows = proj_sheet.get_all_values()
                    today_str = datetime.now().strftime("%Y-%m-%d")

                    # Build a lookup of every existing Sheet 2 record:
                    #   (MODEL NAME upper, date string) -> (1-based row number, current KPI string)
                    # This lets us decide, per (model, day), whether an existing row needs
                    # CHANGING or a brand-new row needs INSERTING. Rows are NEVER deleted.
                    existing_index = {}
                    for r_idx, r_cells in enumerate(proj_rows):
                        if r_idx == 0:  # header row
                            continue
                        nm = r_cells[0].strip().upper() if len(r_cells) > 0 else ""
                        dt = r_cells[1].strip() if len(r_cells) > 1 else ""
                        kp = r_cells[2].strip() if len(r_cells) > 2 else ""
                        if nm and dt:
                            existing_index[(nm, dt)] = (r_idx + 1, kp)

                    pending_updates = []         # (row_number, new_kpi) — only when value differs
                    rows_to_insert_at_top = []   # brand-new [model, date, kpi] rows

                    for p_update in project_sheet_updates:
                        full_name = p_update["model_name"]
                        lq_val = str(p_update["loading_quotient"])
                        clickup_start_str = p_update.get("clickup_start_date", "")
                        clickup_due_str   = p_update.get("clickup_due_date", "")

                        # Projection window = all business days in [start, due] inclusive.
                        # Both bounds come directly from ClickUp (Col K = start, Col L = due),
                        # so the last projected row in Sheet 2 always lands on the exact due date
                        # regardless of how many business days that span contains.
                        clickup_start_dt = clean_and_parse_date(clickup_start_str)
                        clickup_due_dt   = clean_and_parse_date(clickup_due_str)

                        # Current day is always included so an active task logs even if
                        # its window has already elapsed or dates are missing.
                        dates_to_ensure = {today_str}
                        if clickup_start_dt and clickup_due_dt and clickup_due_dt >= clickup_start_dt:
                            # pd.bdate_range(start, end) returns business days in [start, end] inclusive.
                            # This lands the last Sheet 2 row exactly on the ClickUp due date.
                            for bd in pd.bdate_range(str(clickup_start_dt), str(clickup_due_dt)):
                                dates_to_ensure.add(str(bd.date()))

                        # Decide CHANGE vs INSERT for each date (past, present, projected future).
                        for day_str in sorted(dates_to_ensure):
                            key = (full_name.upper(), day_str)
                            if key in existing_index:
                                row_num, current_kpi = existing_index[key]
                                # Only rewrite when the value actually differs. This is exactly
                                # what corrects a future day that was projected earlier with a
                                # now-outdated KPI (e.g. tomorrow's projected value, fixed when
                                # tomorrow's run computes the real KPI).
                                if current_kpi != lq_val:
                                    pending_updates.append((row_num, lq_val))
                                    existing_index[key] = (row_num, lq_val)
                            else:
                                rows_to_insert_at_top.append([full_name, day_str, lq_val])

                    # Apply CHANGES first, while the freshly-read row numbers are still valid.
                    # (Inserts happen at row 2 and shift everything down, so they must follow.)
                    for row_num, new_kpi in pending_updates:
                        execute_with_retry(proj_sheet.update_cell, row_num, 3, new_kpi)
                        print(f" [≠] CONSOLIDATED SHEET METRIC UPDATE: Row {row_num} re-synced to KPI {new_kpi}.")

                    # Insert all new (model, date) rows — historical, current, and projected —
                    # in a single batch at the top. Identical format to every other row.
                    if rows_to_insert_at_top:
                        execute_with_retry(
                            proj_sheet.insert_rows,
                            rows_to_insert_at_top,
                            row=2,
                            value_input_option="USER_ENTERED"
                        )
                        print(f" [✔] CONSOLIDATED SHEET ENTRY INJECTION: Added {len(rows_to_insert_at_top)} "
                              f"logged/projected KPI rows starting at Row 2.")

                    
            except Exception as inner_e:
                print(f"     [!] Error processing row index {idx + 1}: {str(inner_e)}")

        print("\nSUCCESS: Multi-file structural matrix execution complete.")
            
    except Exception as e:
        print(f"\nEngine Failed: {str(e)}")

if __name__ == "__main__":
    main()
