"""
merge_core.py - merge a decoded "minor" apktool tree into a decoded "major" tree.

Handles the mechanical parts that are always the same:
  1. resources (res/) with ID remapping
  2. smali code into fresh smali_classesN folders
  3. assets/ and lib/ (native .so)

The manifest and the auto-start hook are handled in manifest_merge.py.
"""
import os
import re
import shutil
import xml.etree.ElementTree as ET

PREFIX = "minor_"
ARRAY_TAGS = {"string-array", "integer-array", "array"}


def _res_key(el):
    tag = el.tag
    if tag == "item":
        t = el.get("type")
    elif tag in ARRAY_TAGS:
        t = "array"
    else:
        t = tag
    return (t, el.get("name"))


def copy_resources(major, minor, log):
    mres, nres = os.path.join(major, "res"), os.path.join(minor, "res")
    if not os.path.isdir(nres):
        log("[res] minor has no res/ folder")
        return
    skipped_files = skipped_vals = 0
    for d in sorted(os.listdir(nres)):
        src_dir, dst_dir = os.path.join(nres, d), os.path.join(mres, d)
        if not os.path.isdir(src_dir):
            continue
        os.makedirs(dst_dir, exist_ok=True)
        if d.startswith("values"):
            existing = set()
            if os.path.isdir(dst_dir):
                for f in os.listdir(dst_dir):
                    if f.endswith(".xml"):
                        for el in ET.parse(os.path.join(dst_dir, f)).getroot():
                            existing.add(_res_key(el))
            for f in os.listdir(src_dir):
                if f == "public.xml" or not f.endswith(".xml"):
                    continue
                tree = ET.parse(os.path.join(src_dir, f))
                root = tree.getroot()
                for el in list(root):
                    if _res_key(el) in existing:
                        root.remove(el)
                        skipped_vals += 1
                if len(root):
                    tree.write(os.path.join(dst_dir, PREFIX + f),
                               encoding="utf-8", xml_declaration=True)
        else:
            for f in os.listdir(src_dir):
                dst = os.path.join(dst_dir, f)
                if os.path.exists(dst):
                    skipped_files += 1
                else:
                    shutil.copy2(os.path.join(src_dir, f), dst)
    log(f"[res] skipped {skipped_files} duplicate files, {skipped_vals} duplicate values")


def remap_ids(major, minor, log):
    mpub = os.path.join(major, "res/values/public.xml")
    npub = os.path.join(minor, "res/values/public.xml")
    if not (os.path.exists(mpub) and os.path.exists(npub)):
        log("[ids] no public.xml in one of the apps, skipping ID remap")
        return
    mtree = ET.parse(mpub)
    mroot = mtree.getroot()

    by_name, type_id, max_entry = {}, {}, {}
    for e in mroot.findall("public"):
        t, n, i = e.get("type"), e.get("name"), int(e.get("id"), 16)
        by_name[(t, n)] = i
        type_id[t] = (i >> 16) & 0xFF
        max_entry[t] = max(max_entry.get(t, -1), i & 0xFFFF)
    next_type = (max(type_id.values()) + 1) if type_id else 1

    mapping = {}
    for e in ET.parse(npub).getroot().findall("public"):
        t, n, old = e.get("type"), e.get("name"), int(e.get("id"), 16)
        if (t, n) in by_name:
            new = by_name[(t, n)]
        else:
            if t not in type_id:
                type_id[t], max_entry[t] = next_type, -1
                next_type += 1
            max_entry[t] += 1
            new = 0x7F000000 | (type_id[t] << 16) | max_entry[t]
            ET.SubElement(mroot, "public", type=t, name=n, id="0x%08x" % new)
            by_name[(t, n)] = new
        if new != old:
            mapping[old] = new
    mtree.write(mpub, encoding="utf-8", xml_declaration=True)

    pat = re.compile(r"0x7f[0-9a-f]{6}\b")
    changed = 0
    for d in os.listdir(minor):
        if not d.startswith("smali"):
            continue
        for root, _, files in os.walk(os.path.join(minor, d)):
            for f in files:
                if not f.endswith(".smali"):
                    continue
                p = os.path.join(root, f)
                with open(p, encoding="utf-8") as fh:
                    lines = fh.readlines()
                out, dirty = [], False
                for line in lines:
                    nl = pat.sub(lambda m: "0x%08x" % mapping.get(int(m.group(0), 16),
                                                                  int(m.group(0), 16)), line)
                    if nl != line:
                        dirty = True
                        nl = nl.replace("const/high16 ", "const ")
                    out.append(nl)
                if dirty:
                    changed += 1
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.writelines(out)
    log(f"[ids] remapped {len(mapping)} IDs across {changed} smali files")


def copy_smali(major, minor, log):
    major_smali = [d for d in os.listdir(major) if d.startswith("smali")]
    existing = set()
    for d in major_smali:
        base = os.path.join(major, d)
        for root, _, files in os.walk(base):
            for f in files:
                existing.add(os.path.relpath(os.path.join(root, f), base))

    def idx(d):
        return 1 if d == "smali" else int(d.replace("smali_classes", ""))
    n = max((idx(d) for d in major_smali), default=1)

    copied = skipped = 0
    for d in sorted(x for x in os.listdir(minor) if x.startswith("smali")):
        n += 1
        dst_base = os.path.join(major, f"smali_classes{n}")
        src_base = os.path.join(minor, d)
        for root, _, files in os.walk(src_base):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), src_base)
                if rel in existing:
                    skipped += 1
                    continue
                dst = os.path.join(dst_base, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(root, f), dst)
                existing.add(rel)
                copied += 1
    log(f"[smali] copied {copied} classes, skipped {skipped} already present")


def copy_tree_no_overwrite(major, minor, name, log):
    src_base = os.path.join(minor, name)
    if not os.path.isdir(src_base):
        return
    for root, _, files in os.walk(src_base):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), src_base)
            dst = os.path.join(major, name, rel)
            if not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(root, f), dst)
    log(f"[{name}] merged")
