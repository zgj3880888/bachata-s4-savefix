#!/usr/bin/env python3
"""
Surgically replace the guest emulator binary inside a Bachata S4 APK.

Why this shape: the APK does NOT contain a plain `bin/shadps4-arm64`. The whole
Debian-based guest userland ships as ONE stored zip:

    assets/runtime/runtime.zip   111,227,803 B  (397 stored entries)
      host/shadps4-arm64-fex                  <- the emulator core binary
      usr/share/bachata/guest-runtime.json    <- sha256 of that binary
      usr/share/bachata/guest-runtime.txt     <- same, plain text
      usr/share/bachata/shadps4-arm64-fex-needed.txt
    assets/runtime/manifest.json               <- sha256 + size of every entry
    assets/runtime/native-runtime.json         <- JNI .so hashes (untouched)

So a correct swap updates five things coherently: the binary, the two
guest-runtime descriptors, the needed-libs list, and the manifest (including the
derived `runtimeVersion`).

Usage:
    python3 repack-runtime.py <input.apk> <new-core-binary> <output.apk>
                              [--needed needed.txt]
"""

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile

CORE = "host/shadps4-arm64-fex"
GUEST_JSON = "usr/share/bachata/guest-runtime.json"
GUEST_TXT = "usr/share/bachata/guest-runtime.txt"
NEEDED = "usr/share/bachata/shadps4-arm64-fex-needed.txt"
RUNTIME_ZIP = "assets/runtime/runtime.zip"
MANIFEST = "assets/runtime/manifest.json"


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def runtime_version(files):
    """Mirror package-runtime.mjs: sha256 over 'path\\0sha256\\n' for every file."""
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode())
        h.update(b"\0")
        h.update(f["sha256"].encode())
        h.update(b"\n")
    return "runtime-" + h.hexdigest()[:12]


def rebuild_inner_zip(src_bytes, payload):
    """Rewrite runtime.zip in place, keeping entry order / method / timestamps."""
    src = zipfile.ZipFile(io.BytesIO(src_bytes))
    out_buf = io.BytesIO()
    stats = {}
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_STORED) as dst:
        for info in src.infolist():
            data = payload.get(info.filename)
            if data is None:
                data = src.read(info.filename)
            else:
                stats[info.filename] = (info.file_size, len(data))
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            dst.writestr(zi, data)
    missing = set(payload) - set(i.filename for i in src.infolist())
    if missing:
        raise SystemExit(f"payload paths not present in runtime.zip: {sorted(missing)}")
    return out_buf.getvalue(), stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apk")
    ap.add_argument("binary")
    ap.add_argument("output")
    ap.add_argument("--needed", default=None, help="needed.txt produced by the build")
    args = ap.parse_args()

    new_bin = open(args.binary, "rb").read()
    new_sha = sha256(new_bin)
    print(f"new core: {len(new_bin):,} B  sha256={new_sha}")

    outer = zipfile.ZipFile(args.apk)
    names = [i.filename for i in outer.infolist()]
    for required in (RUNTIME_ZIP, MANIFEST):
        if required not in names:
            raise SystemExit(f"{required} not found in APK -- wrong APK version?")

    rz = outer.read(RUNTIME_ZIP)
    inner = zipfile.ZipFile(io.BytesIO(rz))
    inner_names = [i.filename for i in inner.infolist()]
    for required in (CORE, GUEST_JSON, GUEST_TXT, NEEDED):
        if required not in inner_names:
            raise SystemExit(f"{required} not found inside runtime.zip")

    old_sha = sha256(inner.read(CORE))
    print(f"old core sha256={old_sha}  ({inner.getinfo(CORE).file_size:,} B)")

    # ---- descriptor files -------------------------------------------------
    gj = json.loads(inner.read(GUEST_JSON).decode())
    print("guest-runtime.json was:", gj)
    gj["sha256"] = new_sha
    new_gj = (json.dumps(gj, indent=2) + "\n").encode()

    gt = inner.read(GUEST_TXT).decode()
    new_gt = "\n".join(
        f"sha256={new_sha}" if line.startswith("sha256=") else line
        for line in gt.splitlines()
    ).encode() + b"\n"

    if args.needed:
        needed = open(args.needed, "rb").read()
    else:
        needed = inner.read(NEEDED)
    print("needed list:", needed.decode().replace("\n", " "))

    # Every shared object the new binary needs must already live in the runtime,
    # otherwise the guest will fail to start before it ever reaches a save point.
    available = set(inner_names)
    missing_libs = []
    for line in needed.decode().split():
        lib = line.strip()
        if not lib:
            continue
        if lib.startswith("ld-linux"):
            if f"host/{lib}" not in available:
                missing_libs.append(lib)
        elif f"host/{lib}" not in available:
            missing_libs.append(lib)
    if missing_libs:
        raise SystemExit(f"new binary needs libs absent from runtime.zip: {missing_libs}")
    print(f"all {len(needed.decode().split())} needed libs are present in runtime.zip")

    payload = {
        CORE: new_bin,
        GUEST_JSON: new_gj,
        GUEST_TXT: new_gt,
        NEEDED: needed,
    }
    new_rz, stats = rebuild_inner_zip(rz, payload)
    for path, (before, after) in stats.items():
        print(f"  {path}: {before:,} -> {after:,} B")
    print(f"runtime.zip: {len(rz):,} -> {len(new_rz):,} B")

    # ---- manifest ---------------------------------------------------------
    man = json.loads(outer.read(MANIFEST).decode())
    old_ver = man["runtimeVersion"]
    check = runtime_version(man["files"])
    print(f"runtimeVersion: {old_ver} (recomputed {check}) "
          f"{'OK' if check == old_ver else 'MISMATCH - algorithm drift!'}")

    inner_now = zipfile.ZipFile(io.BytesIO(new_rz))
    changed = set()
    for f in man["files"]:
        if f["path"] in payload:
            data = inner_now.read(f["path"])
            f["size"] = len(data)
            f["sha256"] = sha256(data)
            changed.add(f["path"])
    if changed != set(payload):
        raise SystemExit(f"manifest did not cover all payload paths: {changed}")
    man["runtimeVersion"] = runtime_version(man["files"])
    print(f"manifest runtimeVersion -> {man['runtimeVersion']}")

    # sanity: every other manifest entry must still match the archive
    bad = [f["path"] for f in man["files"]
           if f["path"] not in payload and sha256(inner_now.read(f["path"])) != f["sha256"]]
    if bad:
        raise SystemExit(f"manifest/archive mismatch on untouched files: {bad[:5]}")
    print("all untouched manifest entries still match")

    new_man = (json.dumps(man, indent=2) + "\n").encode()

    # ---- outer apk --------------------------------------------------------
    replaced = {RUNTIME_ZIP: new_rz, MANIFEST: new_man}
    with zipfile.ZipFile(args.output, "w") as dst:
        for info in outer.infolist():
            data = replaced.get(info.filename)
            if data is None:
                data = outer.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            dst.writestr(zi, data)
            if info.filename in replaced:
                print(f"  wrote {info.filename}: {info.file_size:,} -> {len(data):,} B")
    print(f"written {args.output}: {os.path.getsize(args.output):,} B")

    # ---- self-check -------------------------------------------------------
    verify = zipfile.ZipFile(args.output)
    v_inner = zipfile.ZipFile(io.BytesIO(verify.read(RUNTIME_ZIP)))
    assert sha256(v_inner.read(CORE)) == new_sha, "core binary mismatch after repack"
    v_man = json.loads(verify.read(MANIFEST).decode())
    by = {f["path"]: f for f in v_man["files"]}
    assert by[CORE]["sha256"] == new_sha
    assert by[CORE]["size"] == len(new_bin)
    assert v_man["runtimeVersion"] == runtime_version(v_man["files"])
    print("SELF-CHECK OK: binary, descriptors and manifest are consistent")


if __name__ == "__main__":
    main()
