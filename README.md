# APK Merger

A small web app: upload a **major** APK and a **minor** APK, get back one
`merged.apk`. The major app keeps its icon and screens; the minor app's
background service is started automatically and has no icon of its own.

> ⚠️ Only merge apps you own or have permission to modify. Re-signing a
> third-party app can breach its licence. This tool is for your own / your
> company's apps.

## Why it can't be a plain static website

The merge needs real tools that only run on a server, not in a browser:
`apktool` (Java), the Android build-tools (`zipalign`, `apksigner`) and
`keytool`. So this is a tiny backend you run on your machine or a cheap VPS,
with a web page for uploading. Nothing to install on the phone.

## What it does automatically

1. Decompiles both APKs with apktool.
2. Merges resources and **remaps resource IDs** so the two apps' code doesn't
   collide (the part that's painful by hand).
3. Copies the minor app's code into fresh `smali_classesN` folders (stays under
   the 64K method limit; skips classes the major app already has).
4. Merges the manifest: adds the minor app's permissions and background
   components, **skips its launcher activity** so it gets no icon.
5. Injects an **auto-start hook** — a generated `ContentProvider`. Android
   creates every declared provider on app launch, before any app code runs, so
   the minor service starts by itself with no edits to either app's code.
6. Rebuilds, zip-aligns and signs → `merged.apk`.

## Setup

Needs Java 17+, then:

```bash
# apktool
curl -L -o apktool.jar https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar
printf '#!/bin/sh\nexec java -jar "%s" "$@"\n' "$PWD/apktool.jar" | sudo tee /usr/local/bin/apktool
sudo chmod +x /usr/local/bin/apktool

# Android build-tools give you zipalign + apksigner (via Android Studio, or the
# command-line "cmdline-tools"). Add them to PATH, e.g.:
export PATH="$PATH:$HOME/Android/Sdk/build-tools/34.0.0"

# Python deps
pip install fastapi "uvicorn[standard]" python-multipart

python3 app.py
```

Open <http://localhost:8000>, drop in the two APKs, click **Merge**.

A signing keystore (`merge.keystore`) is created on first run. For anything real,
replace it with your own key — the APK's identity is tied to the signing key.

## Deploy so others can use it

Any small Linux VPS works. Run `app.py` behind nginx, or:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Put it behind HTTPS and add auth before exposing it publicly — people would be
uploading APKs to it.

## What still won't "just work"

These are limits of APK merging itself, not of this tool:

- **Signature-based features break.** If either app checks its own signature, or
  uses Play Integrity / SafetyNet, Google Maps, or Firebase, those are tied to
  the original signing key and will stop working after re-signing. You'd need to
  register the new key with each service.
- **Android 14+ foreground-service types.** If the minor service is a foreground
  service, newer Android needs `android:foregroundServiceType` and a matching
  permission. The tool copies the service as-declared; if the minor app targeted
  an older Android you may need to add the type by hand in its `<service>` entry.
- **Non-service background work.** If the minor app uses WorkManager or a
  BOOT_COMPLETED receiver instead of a long-running service, there's no service
  to auto-start — the components still get merged, but tell me and I'll adjust
  the hook.
- **Obfuscated / protected apps.** Apps packed with heavy obfuscation or
  anti-tamper (DexGuard and similar) often won't rebuild cleanly.

## Files

- `app.py` — web server + the apktool/zipalign/apksigner pipeline
- `merge_core.py` — resources, ID remap, smali, assets, native libs
- `manifest_merge.py` — manifest merge + the generated auto-start provider
- `templates/index.html` — the upload page
