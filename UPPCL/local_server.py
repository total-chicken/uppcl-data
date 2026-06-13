"""
UPPCL Local Sync Server
Run this on your local machine (same PC where ftp_sync.py lives).
It starts a tiny HTTP server on port 7321 that the browser app can call.

Usage:
    pip install flask requests
    python local_server.py

Then in the app, the "SYNC FROM FTP" button will POST to http://localhost:7321/sync
"""

import subprocess
import sys
import os
import threading
import json
from datetime import datetime

try:
    from flask import Flask, jsonify, request
    import requests
except ImportError:
    print("Installing dependencies (flask, requests)...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask', 'requests'])
    from flask import Flask, jsonify, request
    import requests

app = Flask(__name__)

# Path to ftp_sync.py — assumes it's in the same folder as this file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FTP_SYNC_SCRIPT = os.path.join(SCRIPT_DIR, 'ftp_sync.py')

sync_log = []
sync_running = False


def run_sync_bg(types, force):
    global sync_running, sync_log
    sync_running = True
    sync_log = []
    def log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        sync_log.append(f"[{ts}] {msg}")
        print(msg)
    log(f"Starting sync | types={types} | force={force}")
    args = [sys.executable,
            '-u', FTP_SYNC_SCRIPT, '--types', ','.join(types)]
    if force:
        args.append('--force')
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=SCRIPT_DIR, stdin=subprocess.DEVNULL)
        for line in proc.stdout:
            log(line.rstrip())
        proc.wait()
        log("✅ DONE" if proc.returncode == 0 else f"❌ Code {proc.returncode}")
    except Exception as e:
        log(f"❌ ERROR: {e}")
    finally:
        sync_running = False


@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.route('/sync-status', methods=['GET'])
def sync_status_route():
    try:
        cfg_path = os.path.join(SCRIPT_DIR, 'config.json')
        with open(cfg_path) as f:
            cfg = json.load(f)
        gh = cfg['github']
        import base64, requests as req
        url = f"https://api.github.com/repos/{gh['owner']}/{gh['repo']}/contents/manifest.json"
        r = req.get(url, headers={'Authorization': f"token {gh['token']}",
            'Accept': 'application/vnd.github+json'}, params={'ref': gh['branch']}, timeout=10)
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f"GitHub {r.status_code}"})
        manifest = json.loads(base64.b64decode(r.json()['content']).decode())
        return jsonify({'ok': True, 'sync_status': manifest.get('sync_status', {}),
                        'updated': manifest.get('updated', ''), 'daily_date': manifest.get('daily_date', '')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/sync', methods=['POST', 'OPTIONS'])
def sync():
    global sync_running
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    if sync_running:
        return jsonify({'ok': False, 'message': 'Sync already in progress'})
    if not os.path.exists(FTP_SYNC_SCRIPT):
        return jsonify({'ok': False, 'message': f'ftp_sync.py not found'})
    body = request.get_json(silent=True) or {}
    types = body.get('types', ['master', 'billed', 'unbilled'])
    force = bool(body.get('force', False))
    if not types:
        return jsonify({'ok': False, 'message': 'No types selected'})
    threading.Thread(target=run_sync_bg, args=(types, force), daemon=True).start()
    return jsonify({'ok': True, 'types': types, 'force': force})


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'running': sync_running,
        'log': sync_log[-100:]  # last 100 lines
    })


@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'ok': True, 'script_exists': os.path.exists(FTP_SYNC_SCRIPT)})


if __name__ == '__main__':
    print("=" * 50)
    print("UPPCL Local Sync Server")
    print(f"Script: {FTP_SYNC_SCRIPT}")
    print("Listening on http://localhost:7321")
    print("Keep this window open while using the app.")
    print("=" * 50)
    app.run(host='127.0.0.1', port=7321, debug=False, threaded=True)