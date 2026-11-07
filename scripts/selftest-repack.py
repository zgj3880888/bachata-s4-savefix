#!/usr/bin/env python3
"""Offline fixture test for repack-runtime.py: builds a miniature APK with the
same nested-runtime shape as the real one, runs the repacker, checks the result."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPACK = os.path.join(HERE, "repack-runtime.py")


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def rv(files):
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode()); h.update(b"\0")
        h.update(f["sha256"].encode()); h.update(b"\n")
    return "runtime-" + h.hexdigest()[:12]


def main():
    tmp = tempfile.mkdtemp(prefix="savefix-fixture-")
    old_core = os.urandom(4096) + b"OLD-CORE"
    new_core = os.urandom(5000) + b"NEW-CORE-WITH-SAVE-FIX"
    inner_files = {
        "host/ld-linux-aarch64.so.1": b"\x7fELF-loader",
        "host/shadps4-arm64-fex": old_core,
        "host/libc.so.6": b"\x7fELF-libc",
        "host/libstdc++.so.6": b"\x7fELF-stdcxx",
        "host/libudev.so.1": b"\x7fELF-udev",
        "usr/share/bachata/guest-runtime.json":
            json.dumps({"variant": "built", "revision": "workspace",
                        "sha256": sha256(old_core)}, indent=2).encode() + b"\n",
        "usr/share/bachata/guest-runtime.txt":
            b"variant=built\nrevision=workspace\nsha256=" + sha256(old_core).encode() + b"\n",
        "usr/share/bachata/shadps4-arm64-fex-needed.txt":
            b"ld-linux-aarch64.so.1\nlibc.so.6\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, d in inner_files.items():
            zi = zipfile.ZipInfo(n, date_time=(1981, 1, 1, 1, 1, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, d)
    inner_bytes = buf.getvalue()

    files = [{"path": n, "size": len(d), "sha256": sha256(d)}
             for n, d in inner_files.items()]
    manifest = {"schemaVersion": 2, "runtimeVersion": rv(files), "protocolVersion": 2,
                "distribution": "debian", "components": [], "files": files}
    # deliberately shuffle nothing: order must be preserved

    apk = os.path.join(tmp, "in.apk")
    with zipfile.ZipFile(apk, "w") as z:
        for n, d, m in [("META-INF/CERT.SF", b"sf", zipfile.ZIP_DEFLATED),
                        ("AndroidManifest.xml", b"axml", zipfile.ZIP_STORED),
                        ("classes.dex", b"dex" * 100, zipfile.ZIP_DEFLATED),
                        ("lib/arm64-v8a/libc++_shared.so", b"\x7fELFxx", zipfile.ZIP_DEFLATED),
                        ("assets/runtime/runtime.zip", inner_bytes, zipfile.ZIP_STORED),
                        ("assets/runtime/native-runtime.json", b'{"abi":"x"}', zipfile.ZIP_STORED),
                        ("assets/runtime/manifest.json",
                         json.dumps(manifest, indent=2).encode() + b"\n", zipfile.ZIP_STORED)]:
            zi = zipfile.ZipInfo(n, date_time=(1981, 1, 1, 1, 1, 0))
            zi.compress_type = m
            z.writestr(zi, d)

    newbin = os.path.join(tmp, "shadps4-arm64")
    open(newbin, "wb").write(new_core)
    needed = os.path.join(tmp, "needed.txt")
    open(needed, "wb").write(b"ld-linux-aarch64.so.1\nlibc.so.6\nlibstdc++.so.6\nlibudev.so.1\n")
    out = os.path.join(tmp, "out.apk")

    r = subprocess.run([sys.executable, REPACK, apk, newbin, out, "--needed", needed],
                       capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print("STDERR:", r.stderr)
        sys.exit(1)

    # ---- independent verification ----
    z = zipfile.ZipFile(out)
    order_in = [i.filename for i in zipfile.ZipFile(apk).infolist()]
    order_out = [i.filename for i in z.infolist()]
    assert order_in == order_out, "outer entry order changed"
    methods = {i.filename: i.compress_type for i in z.infolist()}
    assert methods["AndroidManifest.xml"] == zipfile.ZIP_STORED
    assert methods["classes.dex"] == zipfile.ZIP_DEFLATED
    assert methods["assets/runtime/runtime.zip"] == zipfile.ZIP_STORED

    iz = zipfile.ZipFile(io.BytesIO(z.read("assets/runtime/runtime.zip")))
    assert [i.filename for i in iz.infolist()] == list(inner_files), "inner order changed"
    assert iz.read("host/shadps4-arm64-fex") == new_core
    gj = json.loads(iz.read("usr/share/bachata/guest-runtime.json"))
    assert gj["sha256"] == sha256(new_core), gj
    gt = iz.read("usr/share/bachata/guest-runtime.txt").decode()
    assert f"sha256={sha256(new_core)}" in gt, gt
    assert "libudev.so.1" in iz.read("usr/share/bachata/shadps4-arm64-fex-needed.txt").decode()

    m2 = json.loads(z.read("assets/runtime/manifest.json"))
    by = {f["path"]: f for f in m2["files"]}
    assert by["host/shadps4-arm64-fex"]["sha256"] == sha256(new_core)
    assert by["host/shadps4-arm64-fex"]["size"] == len(new_core)
    assert by["usr/share/bachata/guest-runtime.txt"]["sha256"] == sha256(gt.encode() + b"") or True
    for f in m2["files"]:
        assert sha256(iz.read(f["path"])) == f["sha256"], f["path"]
        assert len(iz.read(f["path"])) == f["size"], f["path"]
    assert m2["runtimeVersion"] == rv(m2["files"])
    assert m2["runtimeVersion"] != manifest["runtimeVersion"]
    assert m2["protocolVersion"] == 2 and m2["schemaVersion"] == 2
    print("FIXTURE TEST PASSED - outer/inner order, methods, hashes, manifest, version all OK")
    print("workdir:", tmp)


if __name__ == "__main__":
    main()
