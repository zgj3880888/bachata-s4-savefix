#!/usr/bin/env python3
"""Arm the ARM64 core's startup diagnostics on a checked-out (already ported) core tree.

Three independent, idempotent edits. Each one exists because the previous round could not
tell a hang from a crash, and the strongest suspect is the only HLE library that upstream
registers mid-run.

1. src/main.cpp
   * stdout becomes line buffered. The frontend captures the core's stdout in 4 KiB blocks,
     so any run that dies mid-flight loses its tail -- the round that hung after
     `libSceNgs2` ended on the half word `[Core`, which is exactly a partial block.
   * a detached thread appends `t=<ms> rss= peak= vm= thr=` to
     /sdcard/Download/bachata-logs/bachata-heartbeat.txt every 5 s, open/write/close each
     time so nothing is buffered. A growing file proves the process is alive (hang, or a
     renderer that never presents); a file that stops proves it died. The RSS/thread columns
     make an OOM kill or a thread leak visible without a debugger.

2. src/core/libraries/libs.cpp
   Register libSceNgs2 next to libSceAudioOut in InitHLELibs, i.e. at startup, instead of
   leaving it to the on-demand path in the sysmodule library.

3. src/core/libraries/sysmodule/sysmodule_internal.cpp
   When the game asks for libSceNgs2 and the .sprx is absent, upstream registers the HLE and
   then calls RelocateAllImports() -- a rewrite of every guest import thunk. That runs while
   FEX is already executing guest code, and it is the last thing this core logs. Since (2)
   already registered the library, skip the hot reload; the symbols are in place.

Usage: scripts/apply-diagnostics.py <core-dir>
"""

import pathlib
import sys

MARK_MAIN = b"BachataStartHeartbeat"
MARK_NGS2 = b"libSceNgs2.sprx is pre-registered at startup"


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
MAIN_INCLUDES_OLD = b"""#include <array>
#include <atomic>
"""

MAIN_INCLUDES_NEW = b"""#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
"""

MAIN_ANCHOR_OLD = b"""int main(int argc, char* argv[]) {
"""

MAIN_ANCHOR_NEW = b"""#if defined(__linux__) && defined(ENABLE_BACHATA_RUNTIME)
// The frontend captures stdout in 4 KiB blocks, so a run that dies mid-flight always loses its
// tail. The round that hung after libSceNgs2 ended on the half word "[Core". Line buffering fixes
// that, and an unbuffered heartbeat on its own file answers the question the log cannot: is the
// process alive and spinning, or gone?
namespace {
std::chrono::steady_clock::time_point g_bachata_epoch;

void BachataHeartbeatTick() {
    const int fd = ::open("/sdcard/Download/bachata-logs/bachata-heartbeat.txt",
                          O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        return;
    }

    long rss_kb = 0;
    long peak_kb = 0;
    long vm_kb = 0;
    long threads = 0;
    if (FILE* status = std::fopen("/proc/self/status", "re")) {
        char line[256];
        while (std::fgets(line, sizeof(line), status) != nullptr) {
            if (std::strncmp(line, "VmRSS:", 6) == 0) {
                rss_kb = std::strtol(line + 6, nullptr, 10);
            } else if (std::strncmp(line, "VmHWM:", 6) == 0) {
                peak_kb = std::strtol(line + 6, nullptr, 10);
            } else if (std::strncmp(line, "VmSize:", 7) == 0) {
                vm_kb = std::strtol(line + 7, nullptr, 10);
            } else if (std::strncmp(line, "Threads:", 8) == 0) {
                threads = std::strtol(line + 8, nullptr, 10);
            }
        }
        std::fclose(status);
    }

    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                             std::chrono::steady_clock::now() - g_bachata_epoch)
                             .count();
    char buf[256];
    const int len = std::snprintf(
        buf, sizeof(buf), "t=%8lldms rss=%5ldMB peak=%5ldMB vm=%6ldMB thr=%ld\\n",
        static_cast<long long>(elapsed), rss_kb / 1024, peak_kb / 1024, vm_kb / 1024, threads);
    if (len > 0) {
        ::write(fd, buf, static_cast<size_t>(len));
    }
    ::close(fd);
}

// Must run before anything writes to stdout, hence the split: this part goes first thing in main,
// the heartbeat starts once the log directory exists.
void BachataUnbufferLogs() {
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::setvbuf(stderr, nullptr, _IONBF, 0);
}

void BachataStartHeartbeat() {
    g_bachata_epoch = std::chrono::steady_clock::now();
    std::thread([] {
        BachataHeartbeatTick();
        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(5));
            BachataHeartbeatTick();
        }
    }).detach();
    std::fprintf(stderr, "[bachata-diag] heartbeat armed every 5s\\n");
}
} // namespace
#endif

int main(int argc, char* argv[]) {
#if defined(__linux__) && defined(ENABLE_BACHATA_RUNTIME)
    BachataUnbufferLogs();
#endif
"""

MAIN_HEARTBEAT_OLD = b"""    InstallBachataCrashHandlers();
    BachataSetupReadableLogDir();
    BachataSetupSaveLocation();
#endif
"""

MAIN_HEARTBEAT_NEW = b"""    InstallBachataCrashHandlers();
    BachataSetupReadableLogDir();
    BachataSetupSaveLocation();
    BachataStartHeartbeat();
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
            // rewrites every guest import thunk while FEX is already executing guest code. It
            // is the last thing this core logs before it stops producing output.
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
                    // code -- the exact point this core stops.
                    LOG_INFO(Loader, "libSceNgs2.sprx is pre-registered at startup, skipping "
                                     "hot HLE reload");
                } else {
                    init_func(&linker->GetHLESymbols());

                    // When loading HLEs, we need to relocate imports
                    // This ensures later module loads can see our HLE functions.
                    linker->RelocateAllImports();
                }
"""


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: apply-diagnostics.py <core-dir>")
    core = pathlib.Path(sys.argv[1])

    edit(core / "src/main.cpp",
         [(MAIN_INCLUDES_OLD, MAIN_INCLUDES_NEW), (MAIN_ANCHOR_OLD, MAIN_ANCHOR_NEW),
          (MAIN_HEARTBEAT_OLD, MAIN_HEARTBEAT_NEW)],
         marker=MARK_MAIN, note="(heartbeat + line buffered stdout)")

    edit(core / "src/core/libraries/libs.cpp",
         [(LIBS_INCLUDE_OLD, LIBS_INCLUDE_NEW), (LIBS_REGISTER_OLD, LIBS_REGISTER_NEW)],
         marker=b"Libraries::Ngs2::RegisterLib},",
         note="(libSceNgs2 pre-registered)")

    edit(core / "src/core/libraries/sysmodule/sysmodule_internal.cpp",
         [(SYSMOD_OLD, SYSMOD_NEW)],
         marker=MARK_NGS2, note="(hot HLE reload skipped for Ngs2)")

    # Cheap sanity pass: the strings the CI verifier greps for in the binary.
    print("--- diagnostics check ---")
    for path, needle in (
        (core / "src/main.cpp", MARK_MAIN),
        (core / "src/main.cpp", b"bachata-heartbeat.txt"),
        (core / "src/core/libraries/libs.cpp", b'{"libSceNgs2.sprx", Libraries::Ngs2::RegisterLib}'),
        (core / "src/core/libraries/sysmodule/sysmodule_internal.cpp", MARK_NGS2),
    ):
        ok = needle in path.read_bytes()
        print(f"  {'OK  ' if ok else 'MISS'} {path.relative_to(core)} :: {needle.decode()}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
