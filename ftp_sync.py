"""
UPPCL FTP -> GitHub sync
Accepts CLI args: --types master,billed,unbilled --force
Used by local_server.py (called with JSON payload via args).
"""

import json, gzip, io, csv, base64, sys, argparse
from datetime import datetime, timedelta
from ftplib import FTP, error_perm
import requests

CSV_COLS = ['ACCT_ID','MOBILE_NO','NAME','ADDRESS','CATEGORY','LOAD','LAT','LON',
            'SUBSTATION','FEEDER','LP DATE','LP AMT','TOTAL OUTSTANDING']
MAX_BYTES = 5 * 1024 * 1024

def load_config(path='config.json'):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def ftp_connect(ftp_cfg):
    print(f"Connecting to {ftp_cfg['host']}:{ftp_cfg['port']} ...")
    ftp = FTP()
    ftp.connect(ftp_cfg['host'], ftp_cfg.get('port', 21), timeout=60)
    ftp.login(ftp_cfg['user'], ftp_cfg['pass'])
    print("  Connected OK")
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
    print(f"    -> {len(raw):,} bytes decompressed")
    return raw

def find_master_file(ftp, master_folder, div_code):
    now = datetime.utcnow()
    prev = now.replace(day=1) - timedelta(days=1)
    month_abbr = prev.strftime('%b').upper()
    year = prev.strftime('%Y')
    entries = [e.split('/')[-1] for e in ftp_list(ftp, master_folder)]
    month_dir = next((e for e in entries if e.upper().startswith(month_abbr) and year in e), None)
    if not month_dir:
        raise FileNotFoundError(f"No folder matching {month_abbr}*{year} in {master_folder}")
    folder_path = f"{master_folder}/{month_dir}"
    files = [e.split('/')[-1] for e in ftp_list(ftp, folder_path)]
    target = next((e for e in files if f"DIV{div_code}_" in e.upper() and e.upper().endswith('.CSV.GZ')), None)
    if not target:
        raise FileNotFoundError(f"No master file for DIV{div_code} in {folder_path}")
    return f"{folder_path}/{target}", month_dir

def find_daily_file(ftp, base_folder, prefix, div_code):
    now = datetime.utcnow()
    for delta in (0, 1):
        d = now - timedelta(days=delta)
        ddmmyyyy = d.strftime('%d%m%Y')
        folder_path = f"{base_folder}/{ddmmyyyy}"
        files = [e.split('/')[-1] for e in ftp_list(ftp, folder_path)]
        target = next((e for e in files
                       if e.upper().startswith(prefix.upper())
                       and f"DIV{div_code}_" in e.upper()
                       and e.upper().endswith('.CSV.GZ')), None)
        if target:
            return f"{folder_path}/{target}", ddmmyyyy
    raise FileNotFoundError(f"No {prefix}*DIV{div_code}* for today/yesterday in {base_folder}")

def csv_escape(v):
    v = '' if v is None else str(v)
    return '"' + v.replace('"', '""') + '"' if any(c in v for c in (',', '"', '\n')) else v

def row_line(r):
    return ','.join(csv_escape(r.get(c, '')) for c in CSV_COLS) + '\n'

def parse_master_rows(raw_bytes):
    return list(csv.DictReader(io.StringIO(raw_bytes.decode('utf-8', errors='replace'))))

def shard_master(rows, div_key):
    header = ','.join(CSV_COLS) + '\n'
    chunk_rows, chunk_size, letter, files = [], len(header), 0, []
    def flush():
        nonlocal chunk_rows, chunk_size, letter
        if not chunk_rows: return
        fname = f"master/{div_key}{chr(97+letter)}.csv"
        files.append((fname, header + ''.join(row_line(r) for r in chunk_rows)))
        letter += 1; chunk_rows, chunk_size = [], len(header)
    for r in rows:
        lat, lon = r.get('LAT',''), r.get('LON','')
        try:
            if float(lat) > 50: lat, lon = lon, lat
        except: pass
        row = {c: r.get(c,'') for c in CSV_COLS}
        row['LAT'], row['LON'] = lat, lon
        row['SUBSTATION'] = str(r.get('SUBSTATION','')).strip()
        line = row_line(row)
        if chunk_size + len(line) > MAX_BYTES: flush()
        chunk_rows.append(row); chunk_size += len(line)
    flush()
    return [f for f,_ in files], files

def chunk_passthrough(raw_bytes, folder, prefix):
    lines = raw_bytes.decode('utf-8', errors='replace').splitlines(keepends=True)
    if not lines: return [], []
    header, data_lines = lines[0], lines[1:]
    chunk_lines, chunk_size, letter, files = [], len(header), 0, []
    def flush():
        nonlocal chunk_lines, chunk_size, letter
        if not chunk_lines: return
        fname = f"{folder}/{prefix}{chr(97+letter)}.csv"
        files.append((fname, header + ''.join(chunk_lines)))
        letter += 1; chunk_lines, chunk_size = [], len(header)
    for line in data_lines:
        if chunk_size + len(line) > MAX_BYTES: flush()
        chunk_lines.append(line); chunk_size += len(line)
    flush()
    return [f for f,_ in files], files

def gh_headers(token):
    return {'Authorization': f'token {token}',
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'uppcl-ftp-sync-local'}

def gh_get_file(gh_cfg, path):
    r = requests.get(
        f"https://api.github.com/repos/{gh_cfg['owner']}/{gh_cfg['repo']}/contents/{path}",
        headers=gh_headers(gh_cfg['token']), params={'ref': gh_cfg['branch']})
    return r.json() if r.status_code == 200 else None

def gh_get_json(gh_cfg, path):
    f = gh_get_file(gh_cfg, path)
    return json.loads(base64.b64decode(f['content']).decode('utf-8')) if f else None

def gh_put_file(gh_cfg, path, content_str, message):
    url = f"https://api.github.com/repos/{gh_cfg['owner']}/{gh_cfg['repo']}/contents/{path}"
    existing = gh_get_file(gh_cfg, path)
    body = {'message': message,
            'content': base64.b64encode(content_str.encode('utf-8')).decode('ascii'),
            'branch': gh_cfg['branch']}
    if existing and existing.get('sha'): body['sha'] = existing['sha']
    r = requests.put(url, headers=gh_headers(gh_cfg['token']), json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{path}: {r.status_code} {r.text[:200]}")
    return r.json()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--types', default='master,billed,unbilled',
                        help='Comma-separated: master,billed,unbilled')
    parser.add_argument('--force', action='store_true',
                        help='Skip already-synced checks, re-download everything')
    args = parser.parse_args()

    sync_types = [t.strip() for t in args.types.split(',') if t.strip()]
    force = args.force

    print(f"Sync types: {sync_types}  Force: {force}")

    cfg = load_config()
    ftp_cfg, gh_cfg = cfg['ftp'], cfg['github']
    folders = cfg['folders']
    divisions = cfg['divisions']

    ftp = ftp_connect(ftp_cfg)
    manifest = gh_get_json(gh_cfg, 'manifest.json') or {}
    sync_status = manifest.get('sync_status', {})
    uploads = []

    for div_code, div_key in divisions.items():
        print(f"\n=== DIV{div_code} -> {div_key} ===")
        prev_status = sync_status.get(div_code, {})
        new_status = dict(prev_status)

        # ── MASTER ──────────────────────────────────────────────────────────
        if 'master' in sync_types:
            try:
                path, month_dir = find_master_file(ftp, folders['master'], div_code)
                if not force and prev_status.get('master_period') == month_dir:
                    print(f"  ALREADY SYNCED: master period={month_dir} — skipped (use force to re-download)")
                else:
                    raw = ftp_download_gz(ftp, path)
                    rows = parse_master_rows(raw)
                    print(f"  master rows: {len(rows)}")
                    fnames, files = shard_master(rows, div_key)
                    uploads.extend(files)
                    manifest[div_key] = fnames
                    new_status['master_period'] = month_dir
            except FileNotFoundError as e:
                print(f"  ! master skipped: {e}")

        # ── BILLED ──────────────────────────────────────────────────────────
        if 'billed' in sync_types:
            try:
                path, ddmmyyyy = find_daily_file(ftp, folders['billed'], 'BILLED_', div_code)
                if not force and prev_status.get('billed_date') == ddmmyyyy:
                    print(f"  ALREADY SYNCED: billed date={ddmmyyyy} — skipped (use force to re-download)")
                else:
                    raw = ftp_download_gz(ftp, path)
                    fnames, files = chunk_passthrough(raw, 'billed', f'billed_{div_key}')
                    uploads.extend(files)
                    manifest[f'billed_{div_key}'] = fnames
                    new_status['billed_date'] = ddmmyyyy
            except FileNotFoundError as e:
                print(f"  ! billed skipped: {e}")

        # ── UNBILLED ─────────────────────────────────────────────────────────
        if 'unbilled' in sync_types:
            try:
                path, ddmmyyyy = find_daily_file(ftp, folders['unbilled'], 'UNBILLED_', div_code)
                if not force and prev_status.get('unbilled_date') == ddmmyyyy:
                    print(f"  ALREADY SYNCED: unbilled date={ddmmyyyy} — skipped (use force to re-download)")
                else:
                    raw = ftp_download_gz(ftp, path)
                    fnames, files = chunk_passthrough(raw, 'unbilled', f'unbilled_{div_key}')
                    uploads.extend(files)
                    manifest[f'unbilled_{div_key}'] = fnames
                    new_status['unbilled_date'] = ddmmyyyy
            except FileNotFoundError as e:
                print(f"  ! unbilled skipped: {e}")

        sync_status[div_code] = new_status

    ftp.quit()

    if not uploads:
        print("\nNothing to upload — all already synced or not found.")
        print("DONE")
        return

    manifest['sync_status'] = sync_status
    manifest['updated'] = datetime.utcnow().isoformat() + 'Z'
    manifest['source'] = 'local-ftp-sync'
    manifest['daily_date'] = datetime.utcnow().strftime('%d%m%Y')
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