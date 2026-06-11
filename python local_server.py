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
except ImportError:
    print("Installing flask...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask'])
    from flask import Flask, jsonify, request

app = Flask(__name__)

# Path to ftp_sync.py — assumes it's in the same folder as this file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FTP_SYNC_SCRIPT = os.path.join(SCRIPT_DIR, 'ftp_sync.py')

sync_log = []
sync_running = False


def run_sync_bg():
    global sync_running, sync_log
    sync_running = True
    sync_log = []
    ts = datetime.now().strftime('%H:%M:%S')
    sync_log.append(f"[{ts}] Starting FTP sync...")

    try:
        sync_log.append(f"[debug] Using python: {sys.executable}")
        proc = subprocess.Popen(
            [r"C:\Users\wasif\AppData\Local\Python\pythoncore-3.14-64\python.exe", '-u', FTP_SYNC_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=SCRIPT_DIR,
            # Auto-answer 'n' to all prompts (skip already-synced)
            stdin=subprocess.DEVNULL
        )
        for line in proc.stdout:
            line = line.rstrip()
            ts = datetime.now().strftime('%H:%M:%S')
            sync_log.append(f"[{ts}] {line}")
            print(line)
        proc.wait()
        ts = datetime.now().strftime('%H:%M:%S')
        if proc.returncode == 0:
            sync_log.append(f"[{ts}] ✅ DONE — returncode 0")
        else:
            sync_log.append(f"[{ts}] ❌ Script exited with code {proc.returncode}")
    except Exception as e:
        ts = datetime.now().strftime('%H:%M:%S')
        sync_log.append(f"[{ts}] ❌ ERROR: {str(e)}")
    finally:
        sync_running = False


@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.route('/sync', methods=['POST', 'OPTIONS'])
def sync():
    global sync_running
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    if sync_running:
        return jsonify({'ok': False, 'status': 'already_running',
                        'message': 'Sync already in progress'})
    if not os.path.exists(FTP_SYNC_SCRIPT):
        return jsonify({'ok': False, 'status': 'not_found',
                        'message': f'ftp_sync.py not found at {FTP_SYNC_SCRIPT}'})
    thread = threading.Thread(target=run_sync_bg, daemon=True)
    thread.start()
    return jsonify({'ok': True, 'status': 'started',
                    'message': 'FTP sync started. Poll /status for progress.'})


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