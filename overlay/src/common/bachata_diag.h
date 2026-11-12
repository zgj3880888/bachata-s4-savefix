// SPDX-FileCopyrightText: Copyright 2025-2026 shadPS4 Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Bachata startup diagnostics.
//
// Two things live here:
//   1. process-wide counters bumped from a handful of hot paths (header-only inline variables, so
//      every translation unit shares one instance), and
//   2. a heartbeat thread that appends one line every 5 s to
//      /sdcard/Download/bachata-logs/bachata-heartbeat.txt, written with open/write/close so the
//      frontend's 4 KiB stdout capture cannot truncate it.
//
// The heartbeat is meant to answer one question a plain log cannot: when the picture stays black
// and nothing crashes, is the guest spinning, is it blocked, or is it hammering a function this
// emulator does not implement?
//
//   cpu=%     process CPU over the last period   near 0 = blocked, high = spinning
//   gmain=%   CPU of the "Game:Main" thread      isolates the guest main thread
//   st=R/S/D  thread state histogram             all S + futex wchan = deadlock
//   gst/gw/gsys                                  what the guest main thread is actually doing:
//                                                state, kernel wait point, syscall number
//   busy=     names of threads in R state        says who is burning the CPU
//   w=        top kernel wait points             futex_wait vs ep_poll vs io
//   flip/pres/gnm/blank                          how far the flip pipeline got
//   enosys=   calls into unimplemented HLE functions: total, then the five hottest names
//
// The enosys counters are the interesting new part: the log lists 918 unresolved imports, and a
// guest retrying one of them in a loop looks exactly like a hang.

#pragma once

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string_view>
#include <thread>

#if defined(__linux__)
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace BachataDiag {

// Bumped from the hot paths. Relaxed atomics, no contention concerns.
inline std::atomic<unsigned long long> flips{0};        // sceVideoOutSubmitFlip entry
inline std::atomic<unsigned long long> presents{0};     // Driver::Flip -> presenter->Present
inline std::atomic<unsigned long long> gnm_submits{0};  // sceGnmSubmitCommandBuffers*
inline std::atomic<unsigned long long> blanks{0};       // DrawBlankFrame

// The frontend captures the core's stdout in 4 KiB blocks, so a run that dies or hangs mid-flight
// loses its tail. Line buffering keeps the last lines on disk. Must be called before anything
// writes to stdout.
inline void UnbufferLogs() {
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::setvbuf(stderr, nullptr, _IONBF, 0);
}

#if defined(__linux__)

namespace detail {

constexpr const char* kHeartbeatPath = "/sdcard/Download/bachata-logs/bachata-heartbeat.txt";
constexpr int kTickHz = 100; // CLK_TCK on Android/bionic
constexpr int kPeriodSec = 5;

// ---------------------------------------------------------------------------------------------
// small file helpers: open/read/close, no stdio buffering
// ---------------------------------------------------------------------------------------------

inline int ReadSmallFile(const char* path, char* buf, int cap) {
    buf[0] = '\0';
    const int fd = ::open(path, O_RDONLY);
    if (fd < 0) {
        return 0;
    }
    const ssize_t n = ::read(fd, buf, static_cast<size_t>(cap - 1));
    ::close(fd);
    if (n <= 0) {
        buf[0] = '\0';
        return 0;
    }
    int len = static_cast<int>(n);
    while (len > 0 && (buf[len - 1] == '\x0a' || buf[len - 1] == '\r' || buf[len - 1] == '\0')) {
        --len;
    }
    buf[len] = '\0';
    return len;
}

inline void AppendLine(const char* path, const char* text, int len) {
    if (len <= 0) {
        return;
    }
    const int fd = ::open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        return;
    }
    const ssize_t ignored = ::write(fd, text, static_cast<size_t>(len));
    (void)ignored;
    ::close(fd);
}

// ---------------------------------------------------------------------------------------------
// /proc parsing
// ---------------------------------------------------------------------------------------------

// Parses utime/stime (fields 14/15) out of a /proc/<pid|tid>/stat buffer.
inline bool ParseStatTimes(const char* buf, unsigned long long& utime,
                           unsigned long long& stime) {
    utime = 0;
    stime = 0;
    const char* p = std::strrchr(buf, ')');
    if (p == nullptr) {
        return false;
    }
    ++p;
    int field = 3; // first field after "pid (comm)"
    while (*p != '\0' && field <= 15) {
        while (*p == ' ') {
            ++p;
        }
        if (*p == '\0') {
            break;
        }
        char* end = nullptr;
        const unsigned long long v = std::strtoull(p, &end, 10);
        if (end == p) {
            // Non-numeric field (the state letter), skip the token.
            while (*p != ' ' && *p != '\0') {
                ++p;
            }
        } else {
            p = end;
        }
        if (field == 14) {
            utime = v;
        } else if (field == 15) {
            stime = v;
        }
        ++field;
    }
    return true;
}

inline unsigned long long ReadTicks(const char* path) {
    char buf[512];
    if (ReadSmallFile(path, buf, static_cast<int>(sizeof(buf))) <= 0) {
        return 0;
    }
    unsigned long long utime = 0;
    unsigned long long stime = 0;
    if (!ParseStatTimes(buf, utime, stime)) {
        return 0;
    }
    return utime + stime;
}

inline unsigned long long ReadSelfTicks() {
    return ReadTicks("/proc/self/stat");
}

inline void ReadStatus(long& rss_kb, long& peak_kb, long& vm_kb, long& threads) {
    rss_kb = peak_kb = vm_kb = threads = 0;
    FILE* status = std::fopen("/proc/self/status", "re");
    if (status == nullptr) {
        return;
    }
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

// aarch64 asm-generic numbers, only the ones a stuck guest is likely to sit in.
inline const char* SyscallName(long nr) {
    switch (nr) {
    case 22: return "ep_poll";
    case 35: return "nanosleep";
    case 61: return "write";
    case 62: return "read";
    case 63: return "readv";
    case 64: return "writev";
    case 72: return "pselect6";
    case 73: return "ppoll";
    case 98: return "futex";
    case 113: return "clock_gettime";
    case 115: return "clock_nanosleep";
    case 129: return "kill";
    case 131: return "tgkill";
    case 167: return "prctl";
    case 220: return "clone";
    case 260: return "wait4";
    default: return nullptr;
    }
}

// ---------------------------------------------------------------------------------------------
// thread scan
// ---------------------------------------------------------------------------------------------

struct ThreadScan {
    int running = 0;
    int sleeping = 0;
    int disk = 0;
    int other = 0;
    char busy[3][40] = {};
    int busy_n = 0;
    char wchan[3][40] = {};
    int wchan_count[3] = {};
    int wchan_n = 0;
    bool guest_found = false;
    char g_state = '?';
    char g_wchan[40] = {};
    char g_syscall[28] = {};
    unsigned long long g_ticks = 0;
};

inline void NoteTop(char (*slots)[40], int* counts, int& n, const char* name) {
    for (int i = 0; i < n; ++i) {
        if (std::strncmp(slots[i], name, 39) == 0) {
            ++counts[i];
            return;
        }
    }
    if (n < 3) {
        std::strncpy(slots[n], name, 39);
        slots[n][39] = '\0';
        counts[n] = 1;
        ++n;
    }
}

inline void ScanThreads(ThreadScan& s) {
    DIR* d = ::opendir("/proc/self/task");
    if (d == nullptr) {
        return;
    }
    struct dirent* e;
    char path[128];
    char comm[40];
    char buf[512];
    while ((e = ::readdir(d)) != nullptr) {
        if (e->d_name[0] == '.') {
            continue;
        }

        comm[0] = '\0';
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/comm", e->d_name);
        ReadSmallFile(path, comm, static_cast<int>(sizeof(comm)));

        char state = '?';
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/stat", e->d_name);
        if (ReadSmallFile(path, buf, static_cast<int>(sizeof(buf))) > 0) {
            const char* p = std::strrchr(buf, ')');
            if (p != nullptr && p[1] == ' ') {
                state = p[2];
            }
        }
        switch (state) {
        case 'R': ++s.running; break;
        case 'S': ++s.sleeping; break;
        case 'D': ++s.disk; break;
        default: ++s.other; break;
        }

        if (state == 'R' && comm[0] != '\0' && s.busy_n < 3) {
            std::strncpy(s.busy[s.busy_n], comm, 39);
            s.busy[s.busy_n][39] = '\0';
            ++s.busy_n;
        }

        char wchan[40];
        wchan[0] = '\0';
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/wchan", e->d_name);
        ReadSmallFile(path, wchan, static_cast<int>(sizeof(wchan)));
        if (wchan[0] != '\0' && std::strcmp(wchan, "0") != 0) {
            NoteTop(s.wchan, s.wchan_count, s.wchan_n, wchan);
        }

        if (std::strcmp(comm, "Game:Main") != 0) {
            continue;
        }

        s.guest_found = true;
        s.g_state = state;
        std::strncpy(s.g_wchan, wchan, sizeof(s.g_wchan) - 1);
        s.g_wchan[sizeof(s.g_wchan) - 1] = '\0';

        char sysbuf[28];
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/syscall", e->d_name);
        if (ReadSmallFile(path, sysbuf, static_cast<int>(sizeof(sysbuf))) <= 0) {
            std::strncpy(s.g_syscall, "-", sizeof(s.g_syscall) - 1);
        } else if (std::strncmp(sysbuf, "running", 7) == 0) {
            std::strncpy(s.g_syscall, "run", sizeof(s.g_syscall) - 1);
        } else {
            const long nr = std::strtol(sysbuf, nullptr, 10);
            const char* const nm = SyscallName(nr);
            if (nm != nullptr) {
                std::snprintf(s.g_syscall, sizeof(s.g_syscall), "%s(%ld)", nm, nr);
            } else {
                std::snprintf(s.g_syscall, sizeof(s.g_syscall), "nr%ld", nr);
            }
        }
        s.g_syscall[sizeof(s.g_syscall) - 1] = '\0';

        std::snprintf(path, sizeof(path), "/proc/self/task/%s/stat", e->d_name);
        s.g_ticks += ReadTicks(path);
    }
    ::closedir(d);
}

// ---------------------------------------------------------------------------------------------
// unimplemented HLE call counters
//
// (ptr,len) of the symbol name is packed into one 64-bit word so a slot is installed and read
// atomically, without a lock on the guest's hot path.
// ---------------------------------------------------------------------------------------------

constexpr int kUnresolvedSlots = 64;
constexpr unsigned long long kPtrMask = 0x0000FFFFFFFFFFFFULL;

struct UnresolvedSlot {
    std::atomic<unsigned long long> packed{0};
    std::atomic<unsigned long long> hits{0};
};

inline UnresolvedSlot g_unresolved[kUnresolvedSlots];
inline std::atomic<unsigned long long> g_unresolved_total{0};

inline unsigned HashName(const char* p, unsigned len) {
    unsigned long long h = 1469598103934665603ULL;
    for (unsigned i = 0; i < len; ++i) {
        h ^= static_cast<unsigned char>(p[i]);
        h *= 1099511628211ULL;
    }
    return static_cast<unsigned>(h ^ (h >> 29));
}

inline unsigned NameLen(std::string_view name) {
    return name.size() < 4000u ? static_cast<unsigned>(name.size()) : 4000u;
}

// `name` is the adapter's own std::string member, so the pointer stays valid for the lifetime of
// the process. Slots are installed on the first call, which may race between guest threads; the
// compare-exchange makes that benign.
inline void NoteUnresolvedCall(std::string_view name) {
    g_unresolved_total.fetch_add(1, std::memory_order_relaxed);
    if (name.empty() || name.data() == nullptr) {
        return;
    }
    const unsigned len = NameLen(name);
    const unsigned h = HashName(name.data(), len);
    const unsigned long long want =
        (static_cast<unsigned long long>(len) << 48) |
        (reinterpret_cast<unsigned long long>(name.data()) & kPtrMask);
    for (int i = 0; i < kUnresolvedSlots; ++i) {
        UnresolvedSlot& s = g_unresolved[(h + static_cast<unsigned>(i)) % kUnresolvedSlots];
        unsigned long long cur = s.packed.load(std::memory_order_acquire);
        if (cur == 0) {
            if (s.packed.compare_exchange_strong(cur, want, std::memory_order_acq_rel) ||
                cur == want) {
                s.hits.fetch_add(1, std::memory_order_relaxed);
                return;
            }
        }
        if (static_cast<unsigned>(cur >> 48) != len) {
            continue;
        }
        const char* const sp = reinterpret_cast<const char*>(cur & kPtrMask);
        if (std::memcmp(sp, name.data(), len) == 0) {
            s.hits.fetch_add(1, std::memory_order_relaxed);
            return;
        }
    }
}

inline int ClampAdd(int off, int w, int cap) {
    const int next = off + (w > 0 ? w : 0);
    return next < cap - 1 ? next : cap - 1;
}

// "<total>,<name>:<hits>,..." -- always non-empty so the heartbeat columns never shift.
inline int FormatUnresolved(char* out, int cap) {
    int idx[5] = {-1, -1, -1, -1, -1};
    unsigned long long val[5] = {0, 0, 0, 0, 0};
    for (int i = 0; i < kUnresolvedSlots; ++i) {
        const unsigned long long v = g_unresolved[i].hits.load(std::memory_order_relaxed);
        if (v == 0) {
            continue;
        }
        for (int k = 0; k < 5; ++k) {
            if (v > val[k]) {
                for (int m = 4; m > k; --m) {
                    val[m] = val[m - 1];
                    idx[m] = idx[m - 1];
                }
                val[k] = v;
                idx[k] = i;
                break;
            }
        }
    }
    int off = std::snprintf(out, static_cast<size_t>(cap), "%llu",
                            g_unresolved_total.load(std::memory_order_relaxed));
    if (off < 0) {
        off = 0;
    }
    for (int k = 0; k < 5 && idx[k] >= 0 && off < cap - 1; ++k) {
        const unsigned long long cur = g_unresolved[idx[k]].packed.load(std::memory_order_acquire);
        const unsigned slen = static_cast<unsigned>(cur >> 48);
        const char* const sp = reinterpret_cast<const char*>(cur & kPtrMask);
        char nm[28];
        const unsigned n = slen < 27u ? slen : 27u;
        std::memcpy(nm, sp, n);
        nm[n] = '\0';
        const int w =
            std::snprintf(out + off, static_cast<size_t>(cap - off), ",%s:%llu", nm, val[k]);
        off = ClampAdd(off, w, cap);
    }
    return off;
}

} // namespace detail

inline void NoteUnresolvedCall(std::string_view name) {
    detail::NoteUnresolvedCall(name);
}

inline void StartHeartbeat() {
    static std::atomic<bool> started{false};
    if (started.exchange(true)) {
        return;
    }
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::setvbuf(stderr, nullptr, _IONBF, 0);
    std::fprintf(stderr, "[bachata-diag] heartbeat armed every %ds\x0a", detail::kPeriodSec);

    std::thread([] {
        const auto epoch = std::chrono::steady_clock::now();
        unsigned long long last_cpu = detail::ReadSelfTicks();
        unsigned long long last_guest = 0;
        constexpr unsigned long long ticks_per_period =
            static_cast<unsigned long long>(detail::kTickHz) * detail::kPeriodSec;

        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(detail::kPeriodSec));

            const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - epoch)
                                .count();
            const unsigned long long cpu = detail::ReadSelfTicks();
            const unsigned long long dcpu = cpu >= last_cpu ? cpu - last_cpu : 0;
            last_cpu = cpu;

            long rss_kb = 0;
            long peak_kb = 0;
            long vm_kb = 0;
            long threads = 0;
            detail::ReadStatus(rss_kb, peak_kb, vm_kb, threads);
            detail::ThreadScan scan;
            detail::ScanThreads(scan);

            // CPU% of the guest main thread, isolated from everything else in the process.
            unsigned long long dguest = 0;
            if (scan.guest_found && scan.g_ticks >= last_guest) {
                dguest = scan.g_ticks - last_guest;
                last_guest = scan.g_ticks;
            }
            unsigned long long guest_pct = dguest * 100 / ticks_per_period;
            if (guest_pct > 100) {
                guest_pct = 100;
            }

            char busy[100];
            busy[0] = '\0';
            int off = 0;
            for (int i = 0; i < scan.busy_n && off < 90; ++i) {
                const int w = std::snprintf(busy + off, sizeof(busy) - off, "%s%s",
                                            i == 0 ? "" : ",", scan.busy[i]);
                off = detail::ClampAdd(off, w, static_cast<int>(sizeof(busy)));
            }
            char wchan[128];
            wchan[0] = '\0';
            off = 0;
            for (int i = 0; i < scan.wchan_n && off < 118; ++i) {
                const int w = std::snprintf(wchan + off, sizeof(wchan) - off, "%s%s:%d",
                                            i == 0 ? "" : ",", scan.wchan[i],
                                            scan.wchan_count[i]);
                off = detail::ClampAdd(off, w, static_cast<int>(sizeof(wchan)));
            }
            char enosys[320];
            detail::FormatUnresolved(enosys, static_cast<int>(sizeof(enosys)));

            char line[1200];
            int len = std::snprintf(
                line, sizeof(line),
                "t=%lldms rss=%ldMB peak=%ldMB vm=%ldMB thr=%ld cpu=%llu%% gmain=%llu%% "
                "st=R%d/S%d/D%d gst=%c gw=%s gsys=%s flip=%llu pres=%llu gnm=%llu blank=%llu "
                "enosys=%s busy=%s w=%s",
                static_cast<long long>(ms), rss_kb / 1024, peak_kb / 1024, vm_kb / 1024, threads,
                dcpu * 100 / ticks_per_period, guest_pct, scan.running, scan.sleeping, scan.disk,
                scan.guest_found ? scan.g_state : '-',
                scan.g_wchan[0] != '\0' ? scan.g_wchan : "-",
                scan.g_syscall[0] != '\0' ? scan.g_syscall : "-",
                flips.load(std::memory_order_relaxed), presents.load(std::memory_order_relaxed),
                gnm_submits.load(std::memory_order_relaxed),
                blanks.load(std::memory_order_relaxed), enosys, busy[0] != '\0' ? busy : "-",
                wchan[0] != '\0' ? wchan : "-");
            if (len < 0) {
                continue;
            }
            if (len > static_cast<int>(sizeof(line)) - 2) {
                len = static_cast<int>(sizeof(line)) - 2;
            }
            line[len] = '\x0a';
            ++len;
            detail::AppendLine(detail::kHeartbeatPath, line, len);
        }
    }).detach();
}

#else

inline void NoteUnresolvedCall(std::string_view) {}

inline void StartHeartbeat() {}

#endif

} // namespace BachataDiag
