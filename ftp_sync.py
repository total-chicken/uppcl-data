"""
UPPCL FTP -> GitHub sync (run manually, on demand, while connected to the
SoftEther VPN that has access to ftp.uppclonline.com).

Pulls, per division (config.json -> divisions):
  - Master data (PREVIOUS month - master is finalised after month-end):
      01-MASTER_DATA/<PREV_MONTH>_<YEAR>/DVVNL_DIV<code>_<PREV_MONTH>_<YEAR>.csv.gz
      -> remapped + sharded -> master/eudd1*.csv / master/eudd2*.csv (feeds the tool)
  - Billed (daily, today/yesterday):
      03_CSV_BILLED/<DDMMYYYY>/BILLED_DVVNL_DIV<code>_<DDMMYYYY>.csv.gz
      -> as-is, chunked -> billed/billed_eudd1*.csv / billed/billed_eudd2*.csv
  - Unbilled (daily, today/yesterday):
      04_CSV_UNBILLED/<DDMMYYYY>/UNBILLED_DVVNL_DIV<code>_<DDMMYYYY>.csv.gz
      -> as-is, chunked -> unbilled/unbilled_eudd1*.csv / unbilled/unbilled_eudd2*.csv

Before downloading, checks manifest.json's recorded period for each
division/type. If it matches what's currently on the FTP (i.e. already
synced), it asks for confirmation before re-downloading.

Usage:
    1. Connect SoftEther VPN
    2. Edit config.json (FTP creds, GitHub token)
    3. pip install requests
    4. python ftp_sync.py
"""

import json
import gzip
import io
import csv
import base64
import sys
from datetime import datetime, timedelta
from ftplib import FTP, error_perm

import requests

CSV_COLS = ['ACCT_ID','MOBILE_NO','NAME','ADDRESS','CATEGORY','LOAD','LAT','LON',
            'SUBSTATION','FEEDER','LP DATE','LP AMT','TOTAL OUTSTANDING']
MAX_BYTES = 5 * 1024 * 1024


def load_config(path='config.json'):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def ask_proceed(msg):
    ans = input(f"{msg} Proceed anyway? [y/N]: ").strip().lower()
    return ans == 'y'


# ─── FTP HELPERS ───────────────────────────────────────────────────────────────
def ftp_connect(ftp_cfg):
    print(f"Connecting to {ftp_cfg['host']}:{ftp_cfg['port']} ...")
    ftp = FTP()
    ftp.connect(ftp_cfg['host'], ftp_cfg.get('port', 21), timeout=60)
    ftp.login(ftp_cfg['user'], ftp_cfg['pass'])
    return ftp


def ftp_list(ftp, path):
    try:
        return ftp.nlst(path)
    except error_perm:
        return []


def ftp_download_gz(ftp, remote_path):
    print(f"  Downloading {remote_path} ...")
    buf = io.BytesIO()
    ftp.retrbinary('RETR ' + remote_path, buf.write)
    buf.seek(0)
    raw = gzip.decompress(buf.read())
    print(f"    -> {len(raw):,} bytes after decompress")
    return raw


# ─── PATH RESOLUTION ─────────────────────────────────────────────────────────
def find_master_file(ftp, master_folder, div_code):
    """Master is for the PREVIOUS month (created after month-end)."""
    now = datetime.utcnow()
    prev_month_last_day = now.replace(day=1) - timedelta(days=1)
    month_abbr = prev_month_last_day.strftime('%b').upper()   # MAY, APR
    year = prev_month_last_day.strftime('%Y')

    entries = [e.split('/')[-1] for e in ftp_list(ftp, master_folder)]
    month_dir = None
    for e in entries:
        if e.upper().startswith(month_abbr) and year in e:
            month_dir = e
            break
    if not month_dir:
        raise FileNotFoundError(f"No month folder matching {month_abbr}*{year} in {master_folder} (found: {entries})")

    folder_path = f"{master_folder}/{month_dir}"
    files = [e.split('/')[-1] for e in ftp_list(ftp, folder_path)]
    target = None
    for e in files:
        if f"DIV{div_code}_" in e.upper() and e.upper().endswith('.CSV.GZ'):
            target = e
            break
    if not target:
        raise FileNotFoundError(f"No master file for DIV{div_code} in {folder_path} (found: {files})")

    return f"{folder_path}/{target}", month_dir


def find_daily_file(ftp, base_folder, prefix, div_code):
    now = datetime.utcnow()
    for delta in (0, 1):  # try today, then yesterday
        d = now - timedelta(days=delta)
        ddmmyyyy = d.strftime('%d%m%Y')
        folder_path = f"{base_folder}/{ddmmyyyy}"
        files = [e.split('/')[-1] for e in ftp_list(ftp, folder_path)]
        for e in files:
            if e.upper().startswith(prefix.upper()) and f"DIV{div_code}_" in e.upper() and e.upper().endswith('.CSV.GZ'):
                return f"{folder_path}/{e}", ddmmyyyy
    raise FileNotFoundError(f"No {prefix}*DIV{div_code}* file found in {base_folder} for today/yesterday")


# ─── CSV HELPERS ────────────────────────────────────────────────────────────────
def csv_escape(v):
    if v is None:
        v = ''
    v = str(v)
    if any(c in v for c in (',', '"', '\n')):
        v = '"' + v.replace('"', '""') + '"'
    return v


def row_line(r):
    return ','.join(csv_escape(r.get(c, '')) for c in CSV_COLS) + '\n'


def parse_master_rows(raw_bytes):
    text = raw_bytes.decode('utf-8', errors='replace')
    return list(csv.DictReader(io.StringIO(text)))


def shard_master(rows, div_key):
    header = ','.join(CSV_COLS) + '\n'
    chunk_rows, chunk_size, letter = [], len(header), 0
    files = []

    def flush():
        nonlocal chunk_rows, chunk_size, letter
        if not chunk_rows:
            return
        fname = f"master/{div_key}{chr(97 + letter)}.csv"
        content = header + ''.join(row_line(r) for r in chunk_rows)
        files.append((fname, content))
        letter += 1
        chunk_rows, chunk_size = [], len(header)

    for r in rows:
        sub = str(r.get('SUBSTATION', '')).strip()
        lat, lon = r.get('LAT', ''), r.get('LON', '')
        try:
            if float(lat) > 50:
                lat, lon = lon, lat
        except (TypeError, ValueError):
            pass
        row = {
            'ACCT_ID': r.get('ACCT_ID', ''), 'MOBILE_NO': r.get('MOBILE_NO', ''),
            'NAME': r.get('NAME', ''), 'ADDRESS': r.get('ADDRESS', ''),
            'CATEGORY': r.get('CATEGORY', ''), 'LOAD': r.get('LOAD', ''),
            'LAT': lat, 'LON': lon, 'SUBSTATION': sub, 'FEEDER': r.get('FEEDER', ''),
            'LP DATE': r.get('LP DATE', ''), 'LP AMT': r.get('LP AMT', ''),
            'TOTAL OUTSTANDING': r.get('TOTAL OUTSTANDING', '')
        }
        line = row_line(row)
        if chunk_size + len(line) > MAX_BYTES:
            flush()
        chunk_rows.append(row)
        chunk_size += len(line)
    flush()

    return [f for f, _ in files], files


def chunk_passthrough(raw_bytes, folder, prefix):
    """Split a raw CSV (any schema) into ~5MB chunks, header repeated."""
    text = raw_bytes.decode('utf-8', errors='replace')
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], []
    header = lines[0]
    data_lines = lines[1:]

    files = []
    chunk_lines, chunk_size, letter = [], len(header), 0

    def flush():
        nonlocal chunk_lines, chunk_size, letter
        if not chunk_lines:
            return
        fname = f"{folder}/{prefix}{chr(97 + letter)}.csv"
        content = header + ''.join(chunk_lines)
        files.append((fname, content))
        letter += 1
        chunk_lines, chunk_size = [], len(header)

    for line in data_lines:
        if chunk_size + len(line) > MAX_BYTES:
            flush()
        chunk_lines.append(line)
        chunk_size += len(line)
    flush()

    return [f for f, _ in files], files


# ─── GITHUB ──────────────────────────────────────────────────────────────────
def gh_headers(token):
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'uppcl-ftp-sync-local'
    }


def gh_get_file(gh_cfg, path):
    url = f"https://api.github.com/repos/{gh_cfg['owner']}/{gh_cfg['repo']}/contents/{path}"
    r = requests.get(url, headers=gh_headers(gh_cfg['token']), params={'ref': gh_cfg['branch']})
    if r.status_code != 200:
        return None
    return r.json()


def gh_get_json(gh_cfg, path):
    f = gh_get_file(gh_cfg, path)
    if not f:
        return None
    return json.loads(base64.b64decode(f['content']).decode('utf-8'))


def gh_put_file(gh_cfg, path, content_str, message):
    url = f"https://api.github.com/repos/{gh_cfg['owner']}/{gh_cfg['repo']}/contents/{path}"
    existing = gh_get_file(gh_cfg, path)
    body = {
        'message': message,
        'content': base64.b64encode(content_str.encode('utf-8')).decode('ascii'),
        'branch': gh_cfg['branch']
    }
    if existing and existing.get('sha'):
        body['sha'] = existing['sha']
    r = requests.put(url, headers=gh_headers(gh_cfg['token']), json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{path}: {r.status_code} {r.text[:200]}")
    return r.json()


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()
    ftp_cfg, gh_cfg = cfg['ftp'], cfg['github']
    folders = cfg['folders']
    divisions = cfg['divisions']  # {"233511":"eudd1","233512":"eudd2"}

    ftp = ftp_connect(ftp_cfg)

    manifest = gh_get_json(gh_cfg, 'manifest.json') or {}
    sync_status = manifest.get('sync_status', {})
    uploads = []

    for div_code, div_key in divisions.items():
        print(f"\n=== DIV{div_code} -> {div_key} ===")
        prev_status = sync_status.get(div_code, {})
        new_status = dict(prev_status)

        # Master (previous month)
        try:
            path, month_dir = find_master_file(ftp, folders['master'], div_code)
            if prev_status.get('master_period') == month_dir:
                if not ask_proceed(f"Master for DIV{div_code} period {month_dir} already synced."):
                    print("  -> skipped master")
                    raise StopIteration
            raw = ftp_download_gz(ftp, path)
            rows = parse_master_rows(raw)
            print(f"  master rows: {len(rows)}")
            fnames, files = shard_master(rows, div_key)
            uploads.extend(files)
            manifest[div_key] = fnames
            new_status['master_period'] = month_dir
        except FileNotFoundError as e:
            print(f"  ! master skipped: {e}")
        except StopIteration:
            pass

        # Billed (daily)
        try:
            path, ddmmyyyy = find_daily_file(ftp, folders['billed'], 'BILLED_', div_code)
            if prev_status.get('billed_date') == ddmmyyyy:
                if not ask_proceed(f"Billed for DIV{div_code} date {ddmmyyyy} already synced."):
                    print("  -> skipped billed")
                    raise StopIteration
            raw = ftp_download_gz(ftp, path)
            fnames, files = chunk_passthrough(raw, 'billed', f'billed_{div_key}')
            uploads.extend(files)
            manifest[f'billed_{div_key}'] = fnames
            new_status['billed_date'] = ddmmyyyy
        except FileNotFoundError as e:
            print(f"  ! billed skipped: {e}")
        except StopIteration:
            pass

        # Unbilled (daily)
        try:
            path, ddmmyyyy = find_daily_file(ftp, folders['unbilled'], 'UNBILLED_', div_code)
            if prev_status.get('unbilled_date') == ddmmyyyy:
                if not ask_proceed(f"Unbilled for DIV{div_code} date {ddmmyyyy} already synced."):
                    print("  -> skipped unbilled")
                    raise StopIteration
            raw = ftp_download_gz(ftp, path)
            fnames, files = chunk_passthrough(raw, 'unbilled', f'unbilled_{div_key}')
            uploads.extend(files)
            manifest[f'unbilled_{div_key}'] = fnames
            new_status['unbilled_date'] = ddmmyyyy
        except FileNotFoundError as e:
            print(f"  ! unbilled skipped: {e}")
        except StopIteration:
            pass

        sync_status[div_code] = new_status

    ftp.quit()

    if not uploads:
        print("\nNothing to upload (all skipped / not found). manifest.json not touched.")
        return

    manifest['sync_status'] = sync_status
    manifest['updated'] = datetime.utcnow().isoformat() + 'Z'
    manifest['source'] = 'local-ftp-sync'
    uploads.append(('manifest.json', json.dumps(manifest, indent=2)))

    print(f"\nPushing {len(uploads)} file(s) to GitHub...")
    for fname, content in uploads:
        print(f"  {fname} ({len(content)/1024:.0f} KB) ...")
        gh_put_file(gh_cfg, fname, content, f"FTP sync: update {fname}")
        print("    OK")

    print("\nDONE")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
