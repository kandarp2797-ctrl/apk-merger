# Run it online (nothing installed on your PC)

The app still needs a server with Java + apktool + the Android tools — a browser
can't do that part. "Online" means renting that server in the cloud once. The
included `Dockerfile` installs the whole toolchain automatically, so you never
touch it. After this, it's just a web link.

Everything below is done in a browser.

## Recommended: Render

1. **Put the code on GitHub (in the browser).**
   - Make a free account at github.com.
   - Click **New repository**, name it `apk-merger`, create it.
   - On the repo page, **Add file → Upload files**, and drag in *all* the files
     from the unzipped `apk-merger` folder (including the `Dockerfile` and the
     `templates` folder). Commit.

2. **Deploy on Render.**
   - Make a free account at render.com, sign in with GitHub.
   - **New → Web Service → Build and deploy from a Git repository**, pick your
     `apk-merger` repo.
   - Render detects the `Dockerfile` automatically. Leave the build/start
     commands blank.
   - **Add a password:** under *Environment*, add a variable
     `MERGE_PASSWORD` = something only you know. (The browser will ask for it;
     username can be anything, password is this value.)
   - Create the service. First build takes a few minutes while it installs the
     toolchain.

3. **Use it.** Render gives you a URL like `https://apk-merger-xxxx.onrender.com`.
   Open it, enter the password, upload the two APKs. That's the whole thing,
   from any device.

## Important limits of free cloud tiers

- **Memory.** apktool needs RAM to rebuild. Render's free tier is 512 MB, which
  is fine for small apps but can run out of memory on large APKs (you'd see the
  merge fail). If that happens, bump to the cheapest paid instance (about
  US$7/month, ~2 GB) — plenty.
- **Sleep.** Free services sleep after inactivity, so the first open after a
  while takes ~30 seconds to wake. Paid instances stay awake.
- **Request time.** Merging a big app can take a minute or two; that's within
  Render's limits, but some other hosts cut requests off at 30–60 seconds.

## Other hosts

Anything that builds a Dockerfile works the same way: **Railway**, **Fly.io**,
**Google Cloud Run**, or any Linux VPS (DigitalOcean, etc.) where you run
`docker build` and `docker run`. Render is the least fiddly for a browser-only
setup.

## Run the container locally (optional)

If you ever do have Docker on a machine:

```bash
docker build -t apk-merger .
docker run -p 8000:8000 -e MERGE_PASSWORD=secret apk-merger
# open http://localhost:8000
```
