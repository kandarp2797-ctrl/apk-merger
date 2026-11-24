"""
app.py - APK merger web app.

Upload a "major" APK and a "minor" APK; the server decompiles both with apktool,
merges resources + code + manifest, injects an auto-start hook for the minor
background service, rebuilds, aligns and signs, and returns merged.apk.

Run:
    pip install fastapi "uvicorn[standard]" python-multipart
    python3 app.py
Then open http://localhost:8000

Needs on PATH: apktool, java, zipalign, apksigner, keytool.
See README.md for installing those and for the important caveats.
"""
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import merge_core
import manifest_merge

# Optional gate: set env MERGE_PASSWORD to require a password (any username).
# Leave it unset for local use and the app is open.
PASSWORD = os.environ.get("MERGE_PASSWORD")
_basic = HTTPBasic(auto_error=False)


def auth(creds: HTTPBasicCredentials = Depends(_basic)):
    if not PASSWORD:
        return
    if not creds or not secrets.compare_digest(creds.password, PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(APP_DIR, "jobs")
KEYSTORE = os.path.join(APP_DIR, "merge.keystore")
KS_PASS = "merger123"
KS_ALIAS = "mergerkey"
os.makedirs(WORK, exist_ok=True)

app = FastAPI(title="APK Merger", dependencies=[Depends(auth)])


def run(cmd, log, cwd=None):
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.stdout:
        log(p.stdout.strip())
    if p.returncode != 0:
        log(p.stderr.strip())
        raise RuntimeError(f"command failed: {' '.join(cmd[:2])}")
    return p


def ensure_keystore(log):
    if os.path.exists(KEYSTORE):
        return
    run(["keytool", "-genkeypair", "-v", "-keystore", KEYSTORE,
         "-alias", KS_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
         "-validity", "10000", "-storepass", KS_PASS, "-keypass", KS_PASS,
         "-dname", "CN=APK Merger, O=Merger, C=AU"], log)


def _save(upload: UploadFile, path: str):
    with open(path, "wb") as f:
        shutil.copyfileobj(upload.file, f)


@app.get("/", response_class=HTMLResponse)
def index():
    for p in (os.path.join(APP_DIR, "templates", "index.html"),
              os.path.join(APP_DIR, "index.html")):
        if os.path.exists(p):
            with open(p) as f:
                return f.read()
    raise HTTPException(404, "index.html not found")


@app.post("/services")
async def services(minor: UploadFile = File(...)):
    """Decode just the minor manifest so the UI can offer its services in a dropdown."""
    job = os.path.join(WORK, uuid.uuid4().hex)
    os.makedirs(job)
    logs = []
    try:
        mp = os.path.join(job, "minor.apk")
        _save(minor, mp)
        run(["apktool", "d", "-f", "-s", mp, "-o", os.path.join(job, "minor")],
            logs.append)
        mdir = os.path.join(job, "minor")
        svc = manifest_merge.list_services(mdir)
        sched, hits = manifest_merge.detect_scheduler(mdir)
        return {"services": svc, "scheduler": sched, "hits": hits,
                "app_class": manifest_merge.app_class(mdir)}
    except Exception as e:
        raise HTTPException(500, f"{e}\n" + "\n".join(logs))
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.post("/merge")
async def merge(major: UploadFile = File(...),
                minor: UploadFile = File(...),
                service: str = Form("")):
    job = os.path.join(WORK, uuid.uuid4().hex)
    os.makedirs(job)
    logs = []
    log = logs.append
    try:
        major_apk = os.path.join(job, "major.apk")
        minor_apk = os.path.join(job, "minor.apk")
        _save(major, major_apk)
        _save(minor, minor_apk)

        major_dir = os.path.join(job, "major")
        minor_dir = os.path.join(job, "minor")
        run(["apktool", "d", "-f", major_apk, "-o", major_dir], log)
        run(["apktool", "d", "-f", minor_apk, "-o", minor_dir], log)

        # pick the service to auto-start
        found = manifest_merge.list_services(minor_dir)
        svc = service.strip() or (found[0] if found else "")
        if not svc:
            raise RuntimeError("No <service> found in the minor app, and none was "
                               "chosen. The minor app may do its background work a "
                               "different way (WorkManager/receiver).")

        merge_core.copy_resources(major_dir, minor_dir, log)
        merge_core.remap_ids(major_dir, minor_dir, log)
        merge_core.copy_smali(major_dir, minor_dir, log)
        merge_core.copy_tree_no_overwrite(major_dir, minor_dir, "assets", log)
        merge_core.copy_tree_no_overwrite(major_dir, minor_dir, "lib", log)
        manifest_merge.merge(major_dir, minor_dir, svc, log)

        unsigned = os.path.join(job, "merged-unsigned.apk")
        aligned = os.path.join(job, "merged-aligned.apk")
        signed = os.path.join(job, "merged.apk")
        run(["apktool", "b", major_dir, "-o", unsigned], log)
        run(["zipalign", "-p", "-f", "4", unsigned, aligned], log)
        ensure_keystore(log)
        run(["apksigner", "sign", "--ks", KEYSTORE,
             "--ks-pass", f"pass:{KS_PASS}", "--ks-key-alias", KS_ALIAS,
             "--out", signed, aligned], log)

        sched, _ = manifest_merge.detect_scheduler(minor_dir)
        token = os.path.basename(job)
        return JSONResponse({"ok": True, "download": f"/download/{token}",
                             "service": svc, "scheduler": sched,
                             "log": "\n".join(logs)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "log": "\n".join(logs)},
                            status_code=500)


@app.get("/download/{token}")
def download(token: str):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise HTTPException(400, "bad token")
    path = os.path.join(WORK, token, "merged.apk")
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, filename="merged.apk",
                        media_type="application/vnd.android.package-archive")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
