#!/usr/bin/env python3
"""
Surgically replace the guest emulator binary inside a Bachata S4 / S4+ APK.

Layout facts (verified on the real packages):

  Bachata S4 v0.2.5   assets/runtime/runtime.zip  111,227,803 B  (397 entries, stored)
  Bachata S4+ v0.2.7  assets/runtime/runtime.zip  233,296,550 B  (603 entries, deflated)

  host/shadps4-arm64-fex       <- S4: the emulator core itself
                                  S4+: only a 13 KB dispatcher that execs one of
                                       host/shadps4-arm64-020/023/024/025
  usr/share/bachata/guest-runtime.json / .txt   <- describes the active core
  usr/share/bachata/shadps4-arm64-fex-needed.txt
  assets/runtime/manifest.json                 <- size + sha256 of every entry

Because S4+ ships several cores behind a dispatcher, the entry to patch is not a
constant: pass --core host/shadps4-arm64-024 for S4+ (that is the core the shipped
guest-runtime.json selects) and leave the default for S4.

Everything streams: no intermediate archive is written, because the point of this
script is to run on machines where a 260 MB APK times three does not fit. The new
runtime.zip is produced straight into the output archive.

Usage:
    repack-runtime.py --inspect <apk>
    repack-runtime.py <apk> <new-core> <out.apk> [--core PATH] [--needed FILE]
                      [--allow-missing-descriptors]
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import zipfile

CORE = "host/shadps4-arm64-fex"
CORE_TAG = "core024-c39-fe-clean-r28-savefix"
GUEST_JSON = "usr/share/bachata/guest-runtime.json"
GUEST_TXT = "usr/share/bachata/guest-runtime.txt"
NEEDED = "usr/share/bachata/shadps4-arm64-fex-needed.txt"
RUNTIME_ZIP = "assets/runtime/runtime.zip"
MANIFEST = "assets/runtime/manifest.json"

ELF_MACHINE = {0x03: "x86", 0x3E: "x86-64", 0xB7: "AArch64", 0x28: "ARM"}


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def elf_info(b):
    if len(b) < 20 or b[:4] != b"\x7fELF":
        return "not an ELF"
    cls = {1: "32-bit", 2: "64-bit"}.get(b[4], "?")
    mach = struct.unpack_from("<H", b, 18)[0]
    return f"ELF {cls} {ELF_MACHINE.get(mach, hex(mach))}"


def runtime_version(files):
    """Mirror package-runtime.mjs: sha256 over 'path\\0sha256\\n' for every file."""
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode())
        h.update(b"\0")
        h.update(f["sha256"].encode())
        h.update(b"\n")
    return "runtime-" + h.hexdigest()[:12]


def inspect(apk_path):
    outer = zipfile.ZipFile(apk_path)
    print(f"APK: {apk_path}  ({os.path.getsize(apk_path):,} B, {len(outer.infolist())} entries)")
    if RUNTIME_ZIP not in outer.namelist():
        print("  !! runtime.zip not found; entries mentioning 'runtime':")
        for i in outer.infolist():
            if "runtime" in i.filename:
                print(f"     {i.filename}  {i.file_size:,} B  method={i.compress_type}")
        return 1
    zi = outer.getinfo(RUNTIME_ZIP)
    print(f"  {RUNTIME_ZIP}: stored={zi.compress_size:,} B unpacked={zi.file_size:,} B method={zi.compress_type}")

    if MANIFEST in outer.namelist():
        man = json.loads(outer.read(MANIFEST).decode())
        print(f"  manifest keys: {sorted(man)}")
        print(f"  schemaVersion={man.get('schemaVersion')} runtimeVersion={man.get('runtimeVersion')} "
              f"protocol={man.get('protocolVersion')} files={len(man.get('files', []))}")
        files = man.get("files", [])
        if files:
            print(f"  manifest file keys: {sorted(files[0])}")

    inner = zipfile.ZipFile(io.BytesIO(outer.read(RUNTIME_ZIP)))
    names = inner.namelist()
    print(f"  runtime.zip: {len(names)} entries")
    print("  core candidates:")
    for n in [x for x in names if x.startswith("host/shadps4-arm64")]:
        size = inner.getinfo(n).file_size
        head = inner.read(n)[:64] if size < 200_000_000 else b""
        print(f"    {size:>12,}  {n}  {elf_info(head)}")
    for want in (GUEST_JSON, GUEST_TXT, NEEDED):
        if want in names:
            print(f"    [ok] {want}: {' '.join(inner.read(want).decode('utf-8', 'replace').split())[:180]}")
        else:
            print(f"    [MISSING] {want}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", metavar="APK")
    ap.add_argument("apk", nargs="?")
    ap.add_argument("binary", nargs="?")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--core", action="append", default=None,
                    help="runtime path of the core to replace (repeatable; the first one "
                         "must be the active core that the descriptors point at)")
    ap.add_argument("--needed", default=None)
    ap.add_argument("--label", default=None, help="new guest-runtime label (optional)")
    ap.add_argument("--no-touch-revision", action="store_true",
                    help="keep the original revision string in the descriptors")
    ap.add_argument("--keep-descriptor-sha", action="store_true",
                    help="leave the descriptors' sha256 field untouched (some builds ship a "
                         "constant value unrelated to the active core; the manifest is still "
                         "updated so integrity checks that actually run keep passing)")
    ap.add_argument("--allow-missing-descriptors", action="store_true")
    args = ap.parse_args()

    if args.inspect:
        sys.exit(inspect(args.inspect))
    if not (args.apk and args.binary and args.output):
        ap.error("need <apk> <binary> <output> or --inspect <apk>")

    new_bin = open(args.binary, "rb").read()
    new_sha = sha256(new_bin)
    print(f"new core: {len(new_bin):,} B  {elf_info(new_bin)}  sha256={new_sha}")

    outer = zipfile.ZipFile(args.apk)
    names = [i.filename for i in outer.infolist()]
    for required in (RUNTIME_ZIP, MANIFEST):
        if required not in names:
            raise SystemExit(f"{required} not found in APK -- wrong APK?")

    rz = outer.read(RUNTIME_ZIP)
    inner = zipfile.ZipFile(io.BytesIO(rz))
    inner_names = inner.namelist()

    cores = list(args.core or [CORE])
    core = cores[0]
    for c in cores:
        if c not in inner_names:
            cands = [n for n in inner_names if n.startswith("host/shadps4-arm64")]
            raise SystemExit(f"{c} not found inside runtime.zip; candidates: {cands}")
        old = inner.read(c)
        print(f"old core {c}: sha256={sha256(old)}  ({len(old):,} B)  {elf_info(old)}")
        if elf_info(old) != elf_info(new_bin):
            print("  !! WARNING: ELF class/machine differs from the original binary")
    if len(cores) > 1:
        print(f"replacing {len(cores)} cores with the same binary: {', '.join(cores)}")

    payload = {c: new_bin for c in cores}

    # ---- descriptors ------------------------------------------------------
    if GUEST_JSON not in inner_names and not args.allow_missing_descriptors:
        raise SystemExit(f"{GUEST_JSON} missing; pass --allow-missing-descriptors to proceed")
    if GUEST_JSON in inner_names:
        gj = json.loads(inner.read(GUEST_JSON).decode())
        print("guest-runtime.json was:", gj)
        tag = gj.get("revision", "") if args.no_touch_revision else CORE_TAG
        if not args.keep_descriptor_sha:
            gj["sha256"] = new_sha
        gj["revision"] = tag
        if args.label:
            gj["label"] = args.label
        payload[GUEST_JSON] = (json.dumps(gj, indent=2) + "\n").encode()
    if GUEST_TXT in inner_names:
        gt = inner.read(GUEST_TXT).decode()
        orig_rev = next((l.split("=", 1)[1] for l in gt.splitlines() if l.startswith("revision=")), "")
        tag = orig_rev if args.no_touch_revision else CORE_TAG
        out_lines = []
        for line in gt.splitlines():
            if line.startswith("sha256="):
                out_lines.append(line if args.keep_descriptor_sha else f"sha256={new_sha}")
            elif line.startswith("revision="):
                out_lines.append(f"revision={tag}")
            elif line.startswith("label=") and args.label:
                out_lines.append(f"label={args.label}")
            else:
                out_lines.append(line)
        payload[GUEST_TXT] = ("\n".join(out_lines) + "\n").encode()

    if NEEDED in inner_names:
        needed = open(args.needed, "rb").read() if args.needed else inner.read(NEEDED)
        payload[NEEDED] = needed
        libs = needed.decode().split()
        print("needed list:", " ".join(libs))
        missing = [l for l in libs if f"host/{l}" not in set(inner_names)]
        if missing:
            raise SystemExit(f"new binary needs libs absent from runtime: {missing}")
        print(f"all {len(libs)} needed libs are present")

    # ---- manifest ---------------------------------------------------------
    man = json.loads(outer.read(MANIFEST).decode())
    if "files" not in man or "runtimeVersion" not in man:
        raise SystemExit(f"unexpected manifest schema: {sorted(man)}")
    old_ver = man["runtimeVersion"]
    check = runtime_version(man["files"])
    print(f"runtimeVersion: {old_ver} (recomputed {check}) "
          f"{'OK' if check == old_ver else 'own scheme -> keep prefix'}")

    covered = 0
    for f in man["files"]:
        if f["path"] in payload:
            f["size"] = len(payload[f["path"]])
            f["sha256"] = sha256(payload[f["path"]])
            covered += 1
    if covered != len(payload):
        raise SystemExit(f"manifest covered {covered} of {len(payload)} payload paths")

    bad = [f["path"] for f in man["files"]
           if f["path"] not in payload and sha256(inner.read(f["path"])) != f["sha256"]]
    if bad:
        raise SystemExit(f"manifest/archive mismatch on untouched files: {bad[:5]}")
    print("all untouched manifest entries still match")

    man["runtimeVersion"] = runtime_version(man["files"])
    if check != old_ver:
        # Own version scheme (S4+ ships 'bachata-s4-plus-0.2.7-<12hex>' and derivations
        # such as '...-<12hex>-zd4'): swap only the digest, keep every other component
        # so prefix matching and vendor suffixes survive.
        digest = man["runtimeVersion"].rsplit("-", 1)[-1]
        refreshed = re.sub(r"[0-9a-f]{12,}", digest, old_ver, count=1)
        man["runtimeVersion"] = (
            refreshed if refreshed != old_ver
            else old_ver.rsplit("-", 1)[0] + "-" + digest)
    print(f"manifest runtimeVersion -> {man['runtimeVersion']}")
    new_man = (json.dumps(man, indent=2) + "\n").encode()

    # ---- build the replacement runtime.zip once, on disk ------------------
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="bchrepack_")
    new_rz = os.path.join(tmpdir, "runtime.zip")
    stats = {}
    with zipfile.ZipFile(new_rz, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as rz_out:
        for ii in inner.infolist():
            data = payload.get(ii.filename)
            if data is None:
                data = inner.read(ii.filename)
            else:
                stats[ii.filename] = (ii.file_size, len(data))
            nzi = zipfile.ZipInfo(ii.filename, date_time=ii.date_time)
            nzi.compress_type = ii.compress_type if ii.compress_type in (0, 8) else zipfile.ZIP_DEFLATED
            nzi.external_attr = ii.external_attr
            nzi.create_system = ii.create_system
            rz_out.writestr(nzi, data)
    for p, (b, a) in stats.items():
        print(f"  changed {p}: {b:,} -> {a:,} B")
    print(f"runtime.zip rebuilt: {len(rz):,} -> {os.path.getsize(new_rz):,} B")

    # ---- write the apk ----------------------------------------------------
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as dst:
        for info in outer.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type if info.compress_type in (0, 8) else zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            if info.filename == RUNTIME_ZIP:
                with open(new_rz, "rb") as src, dst.open(zi, "w") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
            elif info.filename == MANIFEST:
                dst.writestr(zi, new_man)
                print(f"  wrote {info.filename}: {info.file_size:,} -> {len(new_man):,} B")
            else:
                with outer.open(info, "r") as src, dst.open(zi, "w") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
    print(f"written {args.output}: {os.path.getsize(args.output):,} B")

    shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- self-check -------------------------------------------------------
    verify = zipfile.ZipFile(args.output)
    v_inner = zipfile.ZipFile(io.BytesIO(verify.read(RUNTIME_ZIP)))
    v_man = json.loads(verify.read(MANIFEST).decode())
    by = {f["path"]: f for f in v_man["files"]}
    for c in cores:
        assert sha256(v_inner.read(c)) == new_sha, f"core binary mismatch after repack: {c}"
        assert by[c]["sha256"] == new_sha, f"manifest sha mismatch: {c}"
        assert by[c]["size"] == len(new_bin), f"manifest size mismatch: {c}"
    assert sha256(v_inner.read(core)) == new_sha, "core binary mismatch after repack"
    bad2 = [f["path"] for f in v_man["files"]
            if f["path"] not in payload and sha256(v_inner.read(f["path"])) != f["sha256"]]
    assert not bad2, f"untouched entries broken after repack: {bad2[:5]}"
    print("SELF-CHECK OK: binary, descriptors and manifest are consistent")


if __name__ == "__main__":
    main()
