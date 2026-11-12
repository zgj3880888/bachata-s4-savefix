#!/usr/bin/env python3
"""Arm the ARM64 core's startup diagnostics on a checked-out (already ported) core tree.

Everything lives in one header that is copied in, so each call site is a single line and the
inline variables give every translation unit the same counter instances.

The counters and the heartbeat answer the question the log cannot. After the libX11 fix the core
gets through SDL, allocates guest memory, spawns guest threads, and then stops producing output
while the process stays alive. `BACHATA_FLIP_TRACE` proves the guest never submitted a single
flip, so nothing is being presented -- but that alone does not say whether the guest is spinning
or blocked. So the heartbeat line carries:

  cpu=%    process CPU over the last 5 s     -> near 0 means blocked, high means spinning
  gmain=%  CPU of the "Game:Main" thread     -> isolates the guest main thread from the rest
  st=R/S/D thread state histogram            -> all S with a futex wchan means deadlock
  gst/gw/gsys                                -> state, wchan and syscall of "Game:Main"
  busy=    names of threads actually running -> says *who* is spinning
  w=       top kernel wait points            -> futex_wait vs epoll_wait vs io
  flip/pres/gnm/blank                      -> how far the flip pipeline got
  enosys=  calls into unimplemented HLE fns  -> total, then the five hottest names

Edits, all idempotent:

1. src/common/bachata_diag.h is copied from overlay/ (new file, header-only).
2. src/main.cpp      -- line-buffered stdout + heartbeat thread start.
3. src/core/libraries/libs.cpp -- register libSceNgs2 at startup.
4. src/core/libraries/sysmodule/sysmodule_internal.cpp -- skip the hot HLE reload of Ngs2,
   which is the only mid-run RelocateAllImports() upstream performs.
5. src/core/libraries/videoout/driver.cpp -- flip/present/blank counters.
6. src/core/libraries/gnmdriver/gnmdriver.cpp -- command buffer submit counter.
7. src/core/guest_cpu/hle_call_adapter.h -- count calls into UnsupportedHleCallAdapter, which is
   the shared stub behind every "unresolved HLE ... temporary ENOSYS fallback".

Usage: scripts/apply-diagnostics.py <core-dir>
"""

import pathlib
import sys

MARK = b"BachataDiag::"

HEADER_SRC = pathlib.Path(__file__).resolve().parent.parent / "overlay/src/common/bachata_diag.h"
HEADER_DST = "src/common/bachata_diag.h"


def read(path):
    p = pathlib.Path(path)
    if not p.is_file():
        sys.exit(f"missing {path}")
    return p


def edit(path, pairs, marker=None, note=""):
    """Apply (old, new) byte pairs to `path`, tolerating either line ending."""
    p = read(path)
    data = p.read_bytes()
    if marker is not None and marker in data:
        print(f"already applied {path} {note}")
        return
    for old, new in pairs:
        hit = None
        for nl in (b"\n", b"\r\n"):
            o = old.replace(b"\n", nl)
            n = new.replace(b"\n", nl)
            count = data.count(o)
            if count == 1:
                data = data.replace(o, n)
                hit = count
                break
            if count > 1:
                sys.exit(f"{path}: ambiguous anchor ({count} matches): {old[:60]!r}")
        if hit is None:
            sys.exit(f"{path}: anchor not found: {old[:80]!r}")
    p.write_bytes(data)
    print(f"applied {path} {note}")


# ---------------------------------------------------------------------------------------------
# 1. src/main.cpp -- line buffered stdout + heartbeat thread
# ---------------------------------------------------------------------------------------------
MAIN_INCLUDE_OLD = b"""#include "common/key_manager.h"
#include "common/logging/log.h"
"""

MAIN_INCLUDE_NEW = b"""#include "common/bachata_diag.h"
#include "common/key_manager.h"
#include "common/logging/log.h"
"""

MAIN_ENTRY_OLD = b"""int main(int argc, char* argv[]) {
"""

MAIN_ENTRY_NEW = b"""int main(int argc, char* argv[]) {
    // Line buffered before anything else writes: the frontend captures stdout in 4 KiB blocks,
    // so an unbuffered tail is what makes a late failure invisible.
    BachataDiag::UnbufferLogs();
"""

MAIN_HEARTBEAT_OLD = b"""    InstallBachataCrashHandlers();
    BachataSetupReadableLogDir();
    BachataSetupSaveLocation();
#endif
"""

MAIN_HEARTBEAT_NEW = b"""    InstallBachataCrashHandlers();
    BachataSetupReadableLogDir();
    BachataSetupSaveLocation();
    // Starts here because the log directory has to exist before the first write.
    BachataDiag::StartHeartbeat();
#endif
"""

# ---------------------------------------------------------------------------------------------
# 2. src/core/libraries/libs.cpp -- register libSceNgs2 at startup
# ---------------------------------------------------------------------------------------------
LIBS_INCLUDE_OLD = b"""#include "core/libraries/net/net.h"
#include "core/libraries/network/http.h"
"""

LIBS_INCLUDE_NEW = b"""#include "core/libraries/net/net.h"
#include "core/libraries/ngs2/ngs2.h"
#include "core/libraries/network/http.h"
"""

LIBS_REGISTER_OLD = b"""            {"libSceAudioOut.sprx", Libraries::AudioOut::RegisterLib},
            {"libSceAudioIn.sprx", Libraries::AudioIn::RegisterLib},
"""

LIBS_REGISTER_NEW = b"""            {"libSceAudioOut.sprx", Libraries::AudioOut::RegisterLib},
            {"libSceAudioIn.sprx", Libraries::AudioIn::RegisterLib},
            // Bachata: registered here rather than on demand. libSceNgs2 is the only sizeable
            // HLE upstream resolves mid-run, and that path ends in RelocateAllImports(), which
            // rewrites every guest import thunk while FEX is already executing guest code.
            {"libSceNgs2.sprx", Libraries::Ngs2::RegisterLib},
"""

# ---------------------------------------------------------------------------------------------
# 3. sysmodule_internal.cpp -- do not hot-reload an already registered Ngs2
# ---------------------------------------------------------------------------------------------
SYSMOD_OLD = b"""                LOG_INFO(Loader, "Can't Load {} switching to HLE", mod_name);
                init_func(&linker->GetHLESymbols());

                // When loading HLEs, we need to relocate imports
                // This ensures later module loads can see our HLE functions.
                linker->RelocateAllImports();
"""

SYSMOD_NEW = b"""                LOG_INFO(Loader, "Can't Load {} switching to HLE", mod_name);
                if (name == "libSceNgs2.sprx") {
                    // Bachata: libSceNgs2 is registered by InitHLELibs at startup, so the hot
                    // path is not needed. Running it here would also call RelocateAllImports(),
                    // rewriting every guest import thunk while FEX is already executing guest
                    // code.
                    LOG_INFO(Loader, "libSceNgs2.sprx is pre-registered at startup, skipping "
                                     "hot HLE reload");
                } else {
                    init_func(&linker->GetHLESymbols());

                    // When loading HLEs, we need to relocate imports
                    // This ensures later module loads can see our HLE functions.
                    linker->RelocateAllImports();
                }
"""

# ---------------------------------------------------------------------------------------------
# 4. videoout/driver.cpp -- flip / present / blank counters
# ---------------------------------------------------------------------------------------------
DRV_INCLUDE_OLD = b"""#include "common/assert.h"
#include "common/debug.h"
"""

DRV_INCLUDE_NEW = b"""#include "common/assert.h"
#include "common/bachata_diag.h"
#include "common/debug.h"
"""

DRV_FLIP_OLD = b"""void VideoOutDriver::Flip(const Request& req) {
    if (present_traces.fetch_add(1, std::memory_order_relaxed) < 32) {
"""

DRV_FLIP_NEW = b"""void VideoOutDriver::Flip(const Request& req) {
    BachataDiag::presents.fetch_add(1, std::memory_order_relaxed);
    if (present_traces.fetch_add(1, std::memory_order_relaxed) < 32) {
"""

DRV_SUBMIT_OLD = b"""                                bool is_eop /*= false*/, u64 lock_generation) {
    if (submit_traces.fetch_add(1, std::memory_order_relaxed) < 32) {
"""

DRV_SUBMIT_NEW = b"""                                bool is_eop /*= false*/, u64 lock_generation) {
    BachataDiag::flips.fetch_add(1, std::memory_order_relaxed);
    if (submit_traces.fetch_add(1, std::memory_order_relaxed) < 32) {
"""

DRV_BLANK_OLD = b"""void VideoOutDriver::DrawBlankFrame() {
"""

DRV_BLANK_NEW = b"""void VideoOutDriver::DrawBlankFrame() {
    BachataDiag::blanks.fetch_add(1, std::memory_order_relaxed);
"""

# ---------------------------------------------------------------------------------------------
# 5. gnmdriver.cpp -- command buffer submit counter
# ---------------------------------------------------------------------------------------------
GNM_INCLUDE_OLD = b"""#include "common/assert.h"
#include "common/debug.h"
"""

GNM_INCLUDE_NEW = b"""#include "common/assert.h"
#include "common/bachata_diag.h"
#include "common/debug.h"
"""

GNM_SUBMIT_OLD = b"""                                                       u32* ccb_sizes_in_bytes) {
    HLE_TRACE;
"""

GNM_SUBMIT_NEW = b"""                                                       u32* ccb_sizes_in_bytes) {
    // Every guest frame starts here; a zero count after minutes of uptime means the guest never
    // reached its first submit, so present() cannot possibly run.
    BachataDiag::gnm_submits.fetch_add(1, std::memory_order_relaxed);
    HLE_TRACE;
"""

# ---------------------------------------------------------------------------------------------
# 6. guest_cpu/hle_call_adapter.h -- count calls into unimplemented imports
# ---------------------------------------------------------------------------------------------
HCA_INCLUDE_OLD = b"""#include "common/types.h"
#include "guest_cpu.h"
"""

HCA_INCLUDE_NEW = b"""#include "common/bachata_diag.h"
#include "common/types.h"
#include "guest_cpu.h"
"""

HCA_INVOKE_OLD = b"""class UnsupportedHleCallAdapter final : public HleCallAdapter {
public:
    HleCallResult Invoke(HleCallFrame&) const override {
        return HleCallFailure{ENOSYS, Name()};
    }
};
"""

HCA_INVOKE_NEW = b"""class UnsupportedHleCallAdapter final : public HleCallAdapter {
public:
    HleCallResult Invoke(HleCallFrame&) const override {
        // Bachata: every "unresolved HLE ... temporary ENOSYS fallback" import lands on this one
        // stub. Counting the calls, keyed by the adapter's own symbol name, is what tells a
        // blocked guest apart from one retrying a call that will never succeed: the second case
        // shows a fast-rising per-symbol count while the picture stays black.
        BachataDiag::NoteUnresolvedCall(Name());
        return HleCallFailure{ENOSYS, Name()};
    }
};
"""


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: apply-diagnostics.py <core-dir>")
    core = pathlib.Path(sys.argv[1])

    # 1. header
    src = read(HEADER_SRC)
    dst = core / HEADER_DST
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload = src.read_bytes().replace(b"\r\n", b"\n")
    if dst.is_file() and dst.read_bytes() == payload:
        print(f"already applied {dst} (diagnostics header)")
    else:
        dst.write_bytes(payload)
        print(f"wrote {dst} ({len(payload)} bytes)")

    # 2. main.cpp
    edit(core / "src/main.cpp",
         [(MAIN_INCLUDE_OLD, MAIN_INCLUDE_NEW), (MAIN_ENTRY_OLD, MAIN_ENTRY_NEW),
          (MAIN_HEARTBEAT_OLD, MAIN_HEARTBEAT_NEW)],
         marker=MARK, note="(heartbeat + line buffered stdout)")

    # 3. libs.cpp
    edit(core / "src/core/libraries/libs.cpp",
         [(LIBS_INCLUDE_OLD, LIBS_INCLUDE_NEW), (LIBS_REGISTER_OLD, LIBS_REGISTER_NEW)],
         marker=b"Libraries::Ngs2::RegisterLib},",
         note="(libSceNgs2 pre-registered)")

    # 4. sysmodule_internal.cpp
    edit(core / "src/core/libraries/sysmodule/sysmodule_internal.cpp",
         [(SYSMOD_OLD, SYSMOD_NEW)],
         marker=b"libSceNgs2.sprx is pre-registered at startup",
         note="(hot HLE reload skipped for Ngs2)")

    # 5. videoout/driver.cpp
    edit(core / "src/core/libraries/videoout/driver.cpp",
         [(DRV_INCLUDE_OLD, DRV_INCLUDE_NEW), (DRV_FLIP_OLD, DRV_FLIP_NEW),
          (DRV_SUBMIT_OLD, DRV_SUBMIT_NEW), (DRV_BLANK_OLD, DRV_BLANK_NEW)],
         marker=b"BachataDiag::presents", note="(flip/present/blank counters)")

    # 6. gnmdriver.cpp
    edit(core / "src/core/libraries/gnmdriver/gnmdriver.cpp",
         [(GNM_INCLUDE_OLD, GNM_INCLUDE_NEW), (GNM_SUBMIT_OLD, GNM_SUBMIT_NEW)],
         marker=b"BachataDiag::gnm_submits", note="(gnm submit counter)")

    # 7. hle_call_adapter.h
    edit(core / "src/core/guest_cpu/hle_call_adapter.h",
         [(HCA_INCLUDE_OLD, HCA_INCLUDE_NEW), (HCA_INVOKE_OLD, HCA_INVOKE_NEW)],
         marker=b"BachataDiag::NoteUnresolvedCall", note="(unimplemented HLE call counter)")

    # Cheap sanity pass: the strings the CI verifier greps for in the binary.
    print("--- diagnostics check ---")
    for path, needle in (
        (core / "src/common/bachata_diag.h", b"bachata-heartbeat.txt"),
        (core / "src/main.cpp", b"BachataDiag::StartHeartbeat"),
        (core / "src/main.cpp", b"BachataDiag::UnbufferLogs"),
        (core / "src/core/libraries/libs.cpp",
         b'{"libSceNgs2.sprx", Libraries::Ngs2::RegisterLib}'),
        (core / "src/core/libraries/sysmodule/sysmodule_internal.cpp",
         b"is pre-registered at startup"),
        (core / "src/core/libraries/videoout/driver.cpp", b"BachataDiag::presents"),
        (core / "src/core/libraries/gnmdriver/gnmdriver.cpp", b"BachataDiag::gnm_submits"),
        (core / "src/core/guest_cpu/hle_call_adapter.h", b"BachataDiag::NoteUnresolvedCall"),
    ):
        ok = needle in path.read_bytes()
        print(f"  {'OK  ' if ok else 'MISS'} {path.relative_to(core)} :: {needle.decode()}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
