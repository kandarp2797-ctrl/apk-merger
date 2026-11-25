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
from fastapi.responses import (HTMLResponse, FileResponse, JSONResponse,
                               StreamingResponse)
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
# Write under a guaranteed-writable temp dir: some hosts (e.g. Hugging Face
# Spaces) run the container as a non-root user without write access to /app.
_BASE = os.path.join(tempfile.gettempdir(), "apkmerge")
WORK = os.path.join(_BASE, "jobs")
# Each APK is uploaded on its own (Cloud Run caps a single request at 32 MiB, so
# the two apps can't be sent together). Staged files live here, keyed by token.
STAGE_DIR = os.path.join(_BASE, "stage")
KEYSTORE = os.path.join(_BASE, "merge.keystore")
KS_PASS = "merger123"
KS_ALIAS = "mergerkey"
os.makedirs(WORK, exist_ok=True)
os.makedirs(STAGE_DIR, exist_ok=True)


def _stage_file(upload):
    token = uuid.uuid4().hex
    d = os.path.join(STAGE_DIR, token)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "app.apk")
    _save(upload, path)
    return token, path

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


# --- Chunked upload: each APK is sent in <32 MB pieces, since Cloud Run caps a
# single request at 32 MiB and a whole APK (or the two together) can exceed that.
@app.post("/stage-init")
async def stage_init():
    token = uuid.uuid4().hex
    d = os.path.join(STAGE_DIR, token)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "app.apk"), "wb").close()
    return {"token": token}


@app.post("/stage-chunk")
async def stage_chunk(token: str = Form(...), chunk: UploadFile = File(...)):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=400)
    path = os.path.join(STAGE_DIR, token, "app.apk")
    if not os.path.isdir(os.path.dirname(path)):
        return JSONResponse({"ok": False, "error": "unknown token"}, status_code=400)
    with open(path, "ab") as f:
        shutil.copyfileobj(chunk.file, f)
    return {"ok": True}


def _staged_apk(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return None
    p = os.path.join(STAGE_DIR, token, "app.apk")
    return p if os.path.exists(p) else None


@app.post("/inspect")
async def inspect(token: str = Form(...)):
    """Decode an already-staged minor APK so the UI can detect its services and
    scheduling entry points for the dropdown."""
    apk = _staged_apk(token)
    if not apk:
        return JSONResponse({"ok": False, "error": "unknown token"}, status_code=400)
    tmp = os.path.join(WORK, uuid.uuid4().hex)
    os.makedirs(tmp)
    logs = []
    try:
        # full decode (no -s): detectors need the smali code, not just the manifest.
        run(["apktool", "d", "-f", apk, "-o", os.path.join(tmp, "minor")],
            logs.append)
        mdir = os.path.join(tmp, "minor")
        sched, hits = manifest_merge.detect_scheduler(mdir)
        return {"services": manifest_merge.list_services(mdir), "scheduler": sched,
                "app_class": manifest_merge.app_class(mdir),
                "bootstraps": manifest_merge.detect_bootstraps(mdir)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{e}\n" + "\n".join(logs)},
                            status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/merge")
async def merge(major_token: str = Form(...),
                minor_token: str = Form(...),
                service: str = Form("")):
    major_apk = _staged_apk(major_token)
    minor_apk = _staged_apk(minor_token)
    if not (major_apk and minor_apk):
        return JSONResponse(
            {"ok": False, "error": "Uploaded APKs weren't found on the server — "
             "re-select both files and try again."}, status_code=400)
    job = os.path.join(WORK, uuid.uuid4().hex)
    os.makedirs(job)
    logs = []
    log = logs.append
    try:
        major_dir = os.path.join(job, "major")
        minor_dir = os.path.join(job, "minor")
        run(["apktool", "d", "-f", major_apk, "-o", major_dir], log)
        run(["apktool", "d", "-f", minor_apk, "-o", minor_dir], log)

        # Decide what the auto-start hook does. Preference order:
        #   1. an explicit choice from the UI (service name OR a "call:" token)
        #   2. the app's own scheduling entry point (best for WorkManager apps)
        #   3. the first declared service (fallback)
        chosen = service.strip()
        if not chosen:
            boots = manifest_merge.detect_bootstraps(minor_dir)
            if boots:
                chosen = boots[0]["token"]
            else:
                svcs = manifest_merge.list_services(minor_dir)
                chosen = svcs[0] if svcs else ""
        if not chosen:
            raise RuntimeError("Couldn't find anything in the minor app to start — "
                               "no scheduling entry point and no <service>. Tell me "
                               "what triggers its background work and I'll wire it.")

        merge_core.copy_resources(major_dir, minor_dir, log)
        merge_core.remap_ids(major_dir, minor_dir, log)
        merge_core.copy_smali(major_dir, minor_dir, log)
        merge_core.copy_tree_no_overwrite(major_dir, minor_dir, "assets", log)
        merge_core.copy_tree_no_overwrite(major_dir, minor_dir, "lib", log)
        manifest_merge.merge(major_dir, minor_dir, chosen, log)

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
                             "service": chosen, "scheduler": sched,
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

    # Stream in chunks with NO Content-Length so the response uses chunked
    # transfer encoding. Cloud Run caps a *buffered* response at 32 MiB, but a
    # streamed one is unbounded — and a merged APK is usually over 32 MiB.
    def gen():
        with open(path, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                yield b

    return StreamingResponse(
        gen(), media_type="application/vnd.android.package-archive",
        headers={"Content-Disposition": 'attachment; filename="merged.apk"'})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
