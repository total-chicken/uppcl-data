"""
UPPCL Local Data Server — Multi-Tenant Edition
Runs on port 7321. Requires VPN connection to ftp.uppclonline.com.

Endpoints:
  GET  /ping       — health check
  GET  /status     — sync log + running flag
  POST /discover   — scan FTP, register division codes in manifest.json
  POST /sync       — download, remap, chunk, upload to GitHub
"""

import os
import io
import csv
import gzip
import json
import base64
import threading
from datetime import datetime, timedelta
from ftplib import FTP, error_perm

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
GH_OWNER  = 'anshu10kumar04-pixel'
GH_REPO   = 'uppcl-data'
GH_BRANCH = 'main'
RAW_BASE  = f'https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/'

FTP_HOST     = 'ftp.uppclonline.com'
FTP_PORT     = 21
FTP_MASTER   = '01-MASTER_DATA'
FTP_BILLED   = '03_CSV_BILLED'
FTP_UNBILLED = '04_CSV_UNBILLED'

CSV_COLS = [
    'ACCT_ID', 'MOBILE_NO', 'NAME', 'ADDRESS', 'CATEGORY', 'LOAD',
    'LAT', 'LON', 'SUBSTATION', 'FEEDER', 'LP DATE', 'LP AMT', 'TOTAL OUTSTANDING'
]
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per chunk

# ─── GLOBALS ─────────────────────────────────────────────────────────────────
SYNC_LOG   = []
IS_RUNNING = False


def log(msg):
    global SYNC_LOG
    t = datetime.now().strftime('%H:%M:%S')
    m = f"[{t}] {msg}"
    print(m)
    SYNC_LOG.append(m)
    if len(SYNC_LOG) > 500:
        SYNC_LOG.pop(0)


# ─── GITHUB HELPERS ───────────────────────────────────────────────────────────
def _gh_headers(pat):
    return {'Authorization': f'token {pat}', 'Accept': 'application/vnd.github+json'}


def gh_get_json(path):
    url = f'https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/{path}'
    r = requests.get(url, timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


def gh_put(path, content_str, pat, message='FTP sync'):
    url = f'https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}'
    headers = _gh_headers(pat)
    sha = None
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        sha = r.json().get('sha')
    body = {
        'message': message,
        'content': base64.b64encode(content_str.encode('utf-8')).decode('ascii'),
        'branch':  GH_BRANCH
    }
    if sha:
        body['sha'] = sha
    r = requests.put(url, headers=headers, json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_discoms():
    d = gh_get_json('discoms.json')
    return d if d else []


def fetch_manifest(pat=None):
    m = gh_get_json('manifest.json')
    if not m:
        m = {'DV': {}, 'MV': {}, 'PV': {}, 'PU': {}}
    return m


# ─── FTP HELPERS ─────────────────────────────────────────────────────────────
def ftp_connect(d):
    ftp = FTP()
    ftp.connect(d.get('ftpServer', FTP_HOST), d.get('ftpPort', FTP_PORT), timeout=60)
    ftp.login(user=d['ftpUser'], passwd=d['ftpPass'])
    return ftp


def ftp_list(ftp, path):
    try:
        entries = ftp.nlst(path)
        return [e.split('/')[-1] for e in entries if e]
    except error_perm:
        return []


def ftp_download_gz(ftp, remote_path):
    buf = io.BytesIO()
    ftp.retrbinary('RETR ' + remote_path, buf.write)
    buf.seek(0)
    return gzip.decompress(buf.read())


def find_master_file(ftp, code_num):
    """
    Find the latest master file for division code `code_num` (e.g. '233511').
    Master lives under: /01-MASTER_DATA/<MON_YEAR>/DVVNL_DIV<code>_<MON_YEAR>.csv.gz
    Returns (remote_path, month_dir).
    """
    month_dirs = ftp_list(ftp, FTP_MASTER)
    month_dirs = [d for d in month_dirs if d and '_' in d]
    if not month_dirs:
        raise FileNotFoundError(f'No month folders in {FTP_MASTER}')
    latest = sorted(month_dirs)[-1]
    folder = f'{FTP_MASTER}/{latest}'
    files = ftp_list(ftp, folder)
    for fname in files:
        if f'DIV{code_num}_' in fname.upper() and fname.upper().endswith('.CSV.GZ'):
            return f'{folder}/{fname}', latest
    raise FileNotFoundError(f'No master file for DIV{code_num} in {folder} (found: {files})')


def find_daily_file(ftp, base_folder, prefix, code_num):
    """
    Try today then yesterday. File pattern: BILLED_DVVNL_DIV<code>_<DDMMYYYY>.csv.gz
    Returns (remote_path, ddmmyyyy).
    """
    now = datetime.utcnow()
    for delta in (0, 1):
        d = now - timedelta(days=delta)
        ddmmyyyy = d.strftime('%d%m%Y')
        folder = f'{base_folder}/{ddmmyyyy}'
        files = ftp_list(ftp, folder)
        for fname in files:
            if (fname.upper().startswith(prefix.upper())
                    and f'DIV{code_num}_' in fname.upper()
                    and fname.upper().endswith('.CSV.GZ')):
                return f'{folder}/{fname}', ddmmyyyy
    raise FileNotFoundError(f'No {prefix}*DIV{code_num}* file found in {base_folder}')


# ─── DATA PROCESSING ─────────────────────────────────────────────────────────
def _find_col(hdrs, options):
    for opt in options:
        try:
            return hdrs.index(opt.upper())
        except ValueError:
            continue
    return -1


def _csv_escape(v):
    v = str(v) if v is not None else ''
    if any(c in v for c in (',', '"', '\n')):
        v = '"' + v.replace('"', '""') + '"'
    return v


def shard_master(rows, discom_id, div_code):
    """
    Remap master data columns, split into 5MB chunks.
    Returns list of (github_path, csv_content_str).
    """
    if not rows:
        return []
    keys = list(rows[0].keys())
    hdrs = [k.strip().upper() for k in keys]

    i_acct = _find_col(hdrs, ['ACCT_ID', 'ACCOUNT_ID'])
    i_mob  = _find_col(hdrs, ['MOBILE_NO', 'MOBILE'])
    i_name = _find_col(hdrs, ['NAME', 'CONSUMER_NAME'])
    i_addr = _find_col(hdrs, ['ADDRESS', 'ADDRES'])
    i_cat  = _find_col(hdrs, ['CATEGORY', 'TARIFF_TYPE', 'SUPPLY_TYPE', 'TARIFF'])
    i_load = _find_col(hdrs, ['LOAD', 'SANCTION_LOAD', 'SANCT_LOAD'])
    i_lat  = _find_col(hdrs, ['LAT', 'LATITUDE'])
    i_lon  = _find_col(hdrs, ['LON', 'LONGITUDE', 'LONG'])
    i_sub  = _find_col(hdrs, ['SUBSTATION', 'SUB_STATION'])
    i_feed = _find_col(hdrs, ['FEEDER'])
    i_date = _find_col(hdrs, ['PAY_DATE', 'LP DATE', 'LAST_PAY_DATE'])
    i_amt  = _find_col(hdrs, ['PAY_AMT', 'LP AMT', 'LAST_PAY_AMT'])
    i_out  = _find_col(hdrs, ['TOTAL_OUTSTANDING', 'TOTAL OUTSTANDING', 'AMOUNT_PAYABLE'])

    header_line = ','.join(CSV_COLS) + '\n'
    files = []
    chunk_lines = []
    chunk_size  = len(header_line)
    letter      = 0

    def flush():
        nonlocal chunk_lines, chunk_size, letter
        if not chunk_lines:
            return
        fname   = f'{discom_id}/{div_code}/master/chunk_{chr(97 + letter)}.csv'
        content = header_line + ''.join(chunk_lines)
        files.append((fname, content))
        letter += 1
        chunk_lines, chunk_size = [], len(header_line)

    for r in rows:
        vals = [r.get(k, '') for k in keys]
        lat = vals[i_lat]  if i_lat  >= 0 else ''
        lon = vals[i_lon]  if i_lon  >= 0 else ''
        try:
            if float(lat) > 50:
                lat, lon = lon, lat
        except (TypeError, ValueError):
            pass

        mapped = {
            'ACCT_ID':           vals[i_acct] if i_acct >= 0 else '',
            'MOBILE_NO':         vals[i_mob]  if i_mob  >= 0 else '',
            'NAME':              vals[i_name] if i_name >= 0 else '',
            'ADDRESS':           vals[i_addr] if i_addr >= 0 else '',
            'CATEGORY':          vals[i_cat]  if i_cat  >= 0 else '',
            'LOAD':              vals[i_load] if i_load >= 0 else '',
            'LAT':               lat,
            'LON':               lon,
            'SUBSTATION':        vals[i_sub]  if i_sub  >= 0 else '',
            'FEEDER':            vals[i_feed] if i_feed >= 0 else '',
            'LP DATE':           vals[i_date] if i_date >= 0 else '',
            'LP AMT':            vals[i_amt]  if i_amt  >= 0 else '',
            'TOTAL OUTSTANDING': vals[i_out]  if i_out  >= 0 else '',
        }
        line = ','.join(_csv_escape(mapped[c]) for c in CSV_COLS) + '\n'
        if chunk_size + len(line) > MAX_BYTES:
            flush()
        chunk_lines.append(line)
        chunk_size += len(line)
    flush()
    return files


def chunk_passthrough(raw_bytes, discom_id, div_code, f_type):
    """
    Split billed/unbilled CSV (any schema) into 5MB chunks, repeating header.
    Returns list of (github_path, csv_content_str).
    """
    text  = raw_bytes.decode('utf-8', errors='replace')
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    header = lines[0]
    files  = []
    chunk_lines = []
    chunk_size  = len(header)
    letter      = 0

    def flush():
        nonlocal chunk_lines, chunk_size, letter
        if not chunk_lines:
            return
        fname   = f'{discom_id}/{div_code}/{f_type}/chunk_{chr(97 + letter)}.csv'
        content = header + ''.join(chunk_lines)
        files.append((fname, content))
        letter += 1
        chunk_lines, chunk_size = [], len(header)

    for line in lines[1:]:
        if chunk_size + len(line) > MAX_BYTES:
            flush()
        chunk_lines.append(line)
        chunk_size += len(line)
    flush()
    return files


# ─── DISCOVERY ────────────────────────────────────────────────────────────────
def run_discovery(pat):
    global IS_RUNNING
    IS_RUNNING = True
    try:
        log('Starting Division Discovery across all DISCOMs...')
        discoms  = fetch_discoms()
        manifest = fetch_manifest(pat)

        for d in discoms:
            d_id = d['id']
            if not isinstance(manifest.get(d_id), dict):
                manifest[d_id] = {}
            try:
                log(f"Connecting to FTP as {d['ftpUser']}...")
                ftp = ftp_connect(d)
                month_dirs = ftp_list(ftp, FTP_MASTER)
                month_dirs = [x for x in month_dirs if x and '_' in x]
                if not month_dirs:
                    log(f'  No month folders for {d_id}')
                    ftp.quit()
                    continue
                latest = sorted(month_dirs)[-1]
                log(f'  Scanning {FTP_MASTER}/{latest} for {d_id}...')
                files = ftp_list(ftp, f'{FTP_MASTER}/{latest}')
                for fname in files:
                    if not fname.upper().endswith('.CSV.GZ'):
                        continue
                    parts = fname.split('_')
                    # DVVNL_DIV233511_MAY_2026.csv.gz → parts[1] = DIV233511
                    if len(parts) < 2 or not parts[1].upper().startswith('DIV'):
                        continue
                    div_code = parts[1].upper()
                    if div_code not in manifest[d_id]:
                        manifest[d_id][div_code] = {'master': [], 'billed': [], 'unbilled': []}
                        log(f'  ✓ Discovered: {d_id}/{div_code}')
                    else:
                        log(f'  ↩ Already known: {d_id}/{div_code}')
                ftp.quit()
            except Exception as e:
                log(f'  ✗ Error for {d_id}: {e}')

        manifest['updated'] = datetime.utcnow().isoformat() + 'Z'
        log('Saving manifest.json to GitHub...')
        gh_put('manifest.json', json.dumps(manifest, indent=2), pat, 'Discovery: register divisions')
        log('Discovery complete. manifest.json updated.')
    except Exception as e:
        log(f'CRITICAL ERROR in discovery: {e}')
    finally:
        IS_RUNNING = False


# ─── SYNC ─────────────────────────────────────────────────────────────────────
def run_sync(types, pat, target_discom=None, target_division=None):
    global IS_RUNNING
    IS_RUNNING = True
    try:
        log(f"Starting sync | types={types} | discom={target_discom} | div={target_division}")
        discoms  = fetch_discoms()
        manifest = fetch_manifest(pat)
        all_uploads = []  # list of (path, content_str)

        for d in discoms:
            if target_discom and d['id'] != target_discom:
                continue
            d_id = d['id']
            if not isinstance(manifest.get(d_id), dict):
                manifest[d_id] = {}

            divs_to_sync = [target_division] if target_division else list(manifest[d_id].keys())
            if not divs_to_sync:
                log(f'No divisions to sync for {d_id}. Run Discovery first.')
                continue

            try:
                log(f"Connecting to FTP as {d['ftpUser']}...")
                ftp = ftp_connect(d)

                for div_code in divs_to_sync:
                    code_num = div_code.upper().replace('DIV', '')
                    if div_code not in manifest[d_id]:
                        manifest[d_id][div_code] = {'master': [], 'billed': [], 'unbilled': []}
                    div_entry = manifest[d_id][div_code]

                    if 'master' in types:
                        try:
                            ftp_path, month_dir = find_master_file(ftp, code_num)
                            log(f'  Downloading master: {ftp_path}')
                            raw  = ftp_download_gz(ftp, ftp_path)
                            rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8', 'replace'))))
                            log(f'  master rows: {len(rows)}')
                            files = shard_master(rows, d_id, div_code)
                            all_uploads.extend(files)
                            div_entry['master'] = [f for f, _ in files]
                            div_entry.setdefault('sync_status', {})['master_period'] = month_dir
                            log(f'  master → {len(files)} chunk(s)')
                        except FileNotFoundError as e:
                            log(f'  master skipped: {e}')

                    if 'billed' in types:
                        try:
                            ftp_path, ddmmyyyy = find_daily_file(ftp, FTP_BILLED, 'BILLED_', code_num)
                            log(f'  Downloading billed: {ftp_path}')
                            raw   = ftp_download_gz(ftp, ftp_path)
                            files = chunk_passthrough(raw, d_id, div_code, 'billed')
                            all_uploads.extend(files)
                            div_entry['billed'] = [f for f, _ in files]
                            div_entry.setdefault('sync_status', {})['billed_date'] = ddmmyyyy
                            log(f'  billed → {len(files)} chunk(s)')
                        except FileNotFoundError as e:
                            log(f'  billed skipped: {e}')

                    if 'unbilled' in types:
                        try:
                            ftp_path, ddmmyyyy = find_daily_file(ftp, FTP_UNBILLED, 'UNBILLED_', code_num)
                            log(f'  Downloading unbilled: {ftp_path}')
                            raw   = ftp_download_gz(ftp, ftp_path)
                            files = chunk_passthrough(raw, d_id, div_code, 'unbilled')
                            all_uploads.extend(files)
                            div_entry['unbilled'] = [f for f, _ in files]
                            div_entry.setdefault('sync_status', {})['unbilled_date'] = ddmmyyyy
                            log(f'  unbilled → {len(files)} chunk(s)')
                        except FileNotFoundError as e:
                            log(f'  unbilled skipped: {e}')

                    div_entry['updated'] = datetime.utcnow().isoformat() + 'Z'
                    log(f'Done: {d_id}/{div_code}')

                ftp.quit()
            except Exception as e:
                log(f'Error for {d_id}: {e}')

        if not all_uploads:
            log('Nothing to upload. Sync complete (no new files).')
            return

        manifest['updated'] = datetime.utcnow().isoformat() + 'Z'
        all_uploads.append(('manifest.json', json.dumps(manifest, indent=2)))
        log(f'Uploading {len(all_uploads)} file(s) to GitHub...')
        for path, content in all_uploads:
            log(f'  → {path}')
            gh_put(path, content, pat, f'FTP sync: {path}')
        log('DONE. All files uploaded.')
    except Exception as e:
        log(f'CRITICAL ERROR in sync: {e}')
    finally:
        IS_RUNNING = False


# ─── API ENDPOINTS ────────────────────────────────────────────────────────────
@app.route('/ping')
def ping():
    return jsonify({'ok': True})


@app.route('/status')
def status():
    return jsonify({'running': IS_RUNNING, 'log': SYNC_LOG})


@app.route('/discover', methods=['POST'])
def discover():
    global IS_RUNNING
    if IS_RUNNING:
        return jsonify({'ok': False, 'message': 'Already running'})
    data = request.json or {}
    pat  = data.get('pat')
    if not pat:
        return jsonify({'ok': False, 'message': 'GitHub PAT required'})
    threading.Thread(target=run_discovery, args=(pat,), daemon=True).start()
    return jsonify({'ok': True, 'message': 'Discovery started'})


@app.route('/sync', methods=['POST'])
def sync():
    global IS_RUNNING
    if IS_RUNNING:
        return jsonify({'ok': False, 'message': 'Sync already in progress'})
    data = request.json or {}
    pat  = data.get('pat')
    if not pat:
        return jsonify({'ok': False, 'message': 'GitHub PAT required'})
    threading.Thread(
        target=run_sync,
        kwargs={
            'types':            data.get('types', ['master', 'billed', 'unbilled']),
            'pat':              pat,
            'target_discom':    data.get('discom'),
            'target_division':  data.get('division'),
        },
        daemon=True
    ).start()
    return jsonify({'ok': True, 'message': 'Sync started'})


if __name__ == '__main__':
    log('UPPCL Data Server starting on port 7321...')
    app.run(port=7321, debug=False)
