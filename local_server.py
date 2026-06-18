import os
import json
import time
import pandas as pd
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from ftplib import FTP
import base64
import threading
from datetime import datetime

app = Flask(__name__)
CORS(app)

# --- GLOBALS ---
SYNC_LOG = []
IS_RUNNING = False
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
GH_OWNER = 'anshu10kumar04-pixel'
GH_REPO = 'uppcl-data'
GH_BRANCH = 'main'

def log(msg):
    global SYNC_LOG
    t = datetime.now().strftime('%H:%M:%S')
    m = f"[{t}] {msg}"
    print(m)
    SYNC_LOG.append(m)
    if len(SYNC_LOG) > 500: SYNC_LOG.pop(0)

# --- GITHUB HELPERS ---
def gh_put(path, content, pat, message="Sync from FTP"):
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json"
    }
    
    # Get current SHA if exists
    sha = None
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        sha = r.json().get('sha')
    
    encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    data = {
        "message": message,
        "content": encoded,
        "branch": GH_BRANCH
    }
    if sha: data["sha"] = sha
    
    r = requests.put(url, headers=headers, json=data)
    if r.status_code not in [200, 201]:
        raise Exception(f"GitHub Error: {r.status_code} - {r.text}")
    return r.json()

# --- FTP & PROCESSING LOGIC ---
def process_sync(types, pat, target_discom=None, target_division=None):
    global IS_RUNNING
    IS_RUNNING = True
    try:
        log(f"Starting sync for {target_discom} / {target_division}")
        
        # 1. Load DISCOMS info from GitHub
        res = requests.get(f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/discoms.json")
        discoms = res.json()
        
        # 2. Iterate through requested DISCOMs
        for d in discoms:
            if target_discom and d['id'] != target_discom: continue
            
            log(f"Connecting to FTP: {d['ftpServer']}...")
            try:
                ftp = FTP(d['ftpServer'])
                ftp.login(user=d['ftpUser'], passwd=d['ftpPass'])
                log(f"Connected to {d['name']}")
                
                files = ftp.nlst()
                # Pattern: DVVNL_DIV233632_MAY_2026.csv
                for fname in files:
                    if not fname.endswith('.csv'): continue
                    parts = fname.replace('.csv','').split('_')
                    if len(parts) < 3: continue
                    
                    discom_name = parts[0]
                    div_code = parts[1] # e.g. DIV233632
                    
                    if target_division and div_code != target_division: continue
                    
                    log(f"Found file: {fname} for Division: {div_code}")
                    
                    # Download file
                    local_tmp = os.path.join(CONFIG_DIR, fname)
                    with open(local_tmp, 'wb') as f:
                        ftp.retrbinary(f"RETR {fname}", f.write)
                    
                    # Process & Split
                    log(f"Splitting {fname} into chunks...")
                    df = pd.read_csv(local_tmp, low_memory=False)
                    
                    # Determine type (master/billed/unbilled) based on content or naming
                    # For now assume master if 'total' in name or fallback
                    f_type = 'master'
                    if 'billed' in fname.lower(): f_type = 'billed'
                    elif 'unbilled' in fname.lower(): f_type = 'unbilled'
                    
                    # Split into 5MB chunks (approx 20,000 rows)
                    chunk_size = 20000
                    chunks = [df[i:i + chunk_size] for i in range(0, df.shape[0], chunk_size)]
                    
                    chunk_names = []
                    for i, chunk in enumerate(chunks):
                        letter = chr(97 + i) # a, b, c...
                        chunk_fname = f"{fname.replace('.csv','')}_{letter}.csv"
                        csv_content = chunk.to_csv(index=False)
                        
                        path = f"{d['id']}/{div_code}/{f_type}/{chunk_fname}"
                        log(f"Uploading {path}...")
                        gh_put(path, csv_content, pat)
                        chunk_names.append(chunk_fname)
                    
                    # Cleanup
                    os.remove(local_tmp)
                    log(f"Successfully processed {fname}")

            except Exception as e:
                log(f"Error processing DISCOM {d['name']}: {str(e)}")
            finally:
                try: ftp.quit()
                except: pass

        log("DONE. Sync process completed.")
    except Exception as e:
        log(f"CRITICAL ERROR: {str(e)}")
    finally:
        IS_RUNNING = False

# --- API ENDPOINTS ---
@app.route('/ping')
def ping(): return jsonify({"ok": True})

@app.route('/status')
def status():
    return jsonify({
        "running": IS_RUNNING,
        "log": SYNC_LOG
    })

@app.route('/sync', methods=['POST'])
def sync():
    global IS_RUNNING
    if IS_RUNNING: return jsonify({"ok": False, "message": "Sync already in progress"})
    
    data = request.json
    pat = data.get('pat')
    if not pat: return jsonify({"ok": False, "message": "GitHub PAT required"})
    
    threading.Thread(target=process_sync, kwargs={
        "types": data.get('types', ['master']),
        "pat": pat,
        "target_discom": data.get('discom'),
        "target_division": data.get('division')
    }).start()
    
    return jsonify({"ok": True, "message": "Sync started"})

@app.route('/discover', methods=['POST'])
def discover():
    # Logic to just scan FTP and update manifest.json on GitHub
    # Used by Super Admin
    data = request.json
    pat = data.get('pat')
    if not pat: return jsonify({"ok": False, "message": "GitHub PAT required"})
    
    def run_discovery():
        try:
            log("Running Division Discovery...")
            res = requests.get(f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/discoms.json")
            discoms = res.json()
            
            manifest_res = requests.get(f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/main/manifest.json")
            manifest = manifest_res.json()
            
            for d in discoms:
                try:
                    ftp = FTP(d['ftpServer'])
                    ftp.login(user=d['ftpUser'], passwd=d['ftpPass'])
                    files = ftp.nlst()
                    
                    d_name = d['name']
                    if d_name not in manifest['divisions']: manifest['divisions'][d_name] = {}
                    
                    for fname in files:
                        if not fname.endswith('.csv'): continue
                        parts = fname.replace('.csv','').split('_')
                        if len(parts) < 2: continue
                        div_code = parts[1]
                        
                        if div_code not in manifest['divisions'][d_name]:
                            manifest['divisions'][d_name][div_code] = {"master":[], "billed":[], "unbilled":[]}
                            log(f"Discovered new division: {div_code} in {d_name}")
                    
                    ftp.quit()
                except Exception as e: log(f"Discovery error for {d['name']}: {str(e)}")
            
            manifest['last_discovered'] = datetime.now().isoformat()
            gh_put("manifest.json", json.dumps(manifest, indent=2), pat, "Update manifest via Discovery")
            log("Discovery complete. Manifest updated on GitHub.")
        except Exception as e: log(f"Discovery failed: {str(e)}")
        
    threading.Thread(target=run_discovery).start()
    return jsonify({"ok": True, "message": "Discovery started"})

if __name__ == '__main__':
    log("UPPCL Data Server Starting on port 7321...")
    app.run(port=7321, debug=False)
