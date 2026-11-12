// SPDX-FileCopyrightText: Copyright 2025-2026 shadPS4 Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Bachata startup diagnostics: process-wide counters plus a heartbeat thread that appends one
// line every 5 seconds to /sdcard/Download/bachata-logs/bachata-heartbeat.txt.
//
// Header-only on purpose: every translation unit that bumps a counter shares the same instance
// through inline variables, and no build-system change is needed.

#pragma once

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
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

// Parses utime/stime (fields 14/15) out of /proc/<pid|tid>/stat.
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

inline unsigned long long ReadProcTicks(const char* path) {
    FILE* f = std::fopen(path, "re");
    if (f == nullptr) {
        return 0;
    }
    char buf[1024];
    const size_t n = std::fread(buf, 1, sizeof(buf) - 1, f);
    std::fclose(f);
    if (n == 0) {
        return 0;
    }
    buf[n] = '\0';
    unsigned long long utime = 0;
    unsigned long long stime = 0;
    if (!ParseStatTimes(buf, utime, stime)) {
        return 0;
    }
    return utime + stime;
}

inline unsigned long long ReadSelfTicks() {
    return ReadProcTicks("/proc/self/stat");
}

// CPU ticks burned by the guest main thread (named "Game:Main" by the port) so far.
inline unsigned long long ReadGuestMainTicks() {
    DIR* d = ::opendir("/proc/self/task");
    if (d == nullptr) {
        return 0;
    }
    unsigned long long total = 0;
    struct dirent* e;
    while ((e = ::readdir(d)) != nullptr) {
        if (e->d_name[0] == '.') {
            continue;
        }
        char path[128];
        char comm[64] = {};
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/comm", e->d_name);
        FILE* cf = std::fopen(path, "re");
        if (cf == nullptr) {
            continue;
        }
        if (std::fgets(comm, sizeof(comm), cf) == nullptr) {
            comm[0] = '\0';
        }
        std::fclose(cf);
        if (char* nl = std::strchr(comm, '\n'); nl != nullptr) {
            *nl = '\0';
        }
        if (std::strcmp(comm, "Game:Main") != 0) {
            continue;
        }
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/stat", e->d_name);
        total += ReadProcTicks(path);
    }
    ::closedir(d);
    return total;
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
    while ((e = ::readdir(d)) != nullptr) {
        if (e->d_name[0] == '.') {
            continue;
        }
        char path[128];

        char state = '?';
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/stat", e->d_name);
        FILE* sf = std::fopen(path, "re");
        if (sf != nullptr) {
            char buf[512];
            const size_t n = std::fread(buf, 1, sizeof(buf) - 1, sf);
            std::fclose(sf);
            buf[n] = '\0';
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

        char comm[40] = {};
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/comm", e->d_name);
        FILE* cf = std::fopen(path, "re");
        if (cf != nullptr) {
            if (std::fgets(comm, sizeof(comm), cf) == nullptr) {
                comm[0] = '\0';
            }
            std::fclose(cf);
            if (char* nl = std::strchr(comm, '\n'); nl != nullptr) {
                *nl = '\0';
            }
        }
        if (state == 'R' && comm[0] != '\0' && s.busy_n < 3) {
            std::strncpy(s.busy[s.busy_n], comm, 39);
            s.busy[s.busy_n][39] = '\0';
            ++s.busy_n;
        }

        char wchan[40] = {};
        std::snprintf(path, sizeof(path), "/proc/self/task/%s/wchan", e->d_name);
        FILE* wf = std::fopen(path, "re");
        if (wf != nullptr) {
            if (std::fgets(wchan, sizeof(wchan), wf) == nullptr) {
                wchan[0] = '\0';
            }
            std::fclose(wf);
            if (char* nl = std::strchr(wchan, '\n'); nl != nullptr) {
                *nl = '\0';
            }
        }
        if (wchan[0] != '\0' && std::strcmp(wchan, "0") != 0) {
            NoteTop(s.wchan, s.wchan_count, s.wchan_n, wchan);
        }
    }
    ::closedir(d);
}

inline void AppendLine(const char* text, int len) {
    const int fd =
        ::open(kHeartbeatPath, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        return;
    }
    const ssize_t ignored = ::write(fd, text, static_cast<size_t>(len));
    (void)ignored;
    ::close(fd);
}

} // namespace detail

inline void StartHeartbeat() {
    static std::atomic<bool> started{false};
    if (started.exchange(true)) {
        return;
    }
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::setvbuf(stderr, nullptr, _IONBF, 0);
    std::fprintf(stderr, "[bachata-diag] heartbeat armed every %ds\n", detail::kPeriodSec);

    std::thread([] {
        const auto epoch = std::chrono::steady_clock::now();
        unsigned long long last_cpu = detail::ReadSelfTicks();
        unsigned long long last_guest = detail::ReadGuestMainTicks();
        constexpr unsigned long long ticks_per_period =
            static_cast<unsigned long long>(detail::kTickHz) * detail::kPeriodSec;

        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(detail::kPeriodSec));

            const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - epoch)
                                .count();
            const unsigned long long cpu = detail::ReadSelfTicks();
            const unsigned long long guest = detail::ReadGuestMainTicks();
            const unsigned long long dcpu = cpu >= last_cpu ? cpu - last_cpu : 0;
            const unsigned long long dguest = guest >= last_guest ? guest - last_guest : 0;
            last_cpu = cpu;
            last_guest = guest;

            long rss_kb = 0;
            long peak_kb = 0;
            long vm_kb = 0;
            long threads = 0;
            detail::ReadStatus(rss_kb, peak_kb, vm_kb, threads);
            detail::ThreadScan scan;
            detail::ScanThreads(scan);

            char busy[100] = {};
            int off = 0;
            for (int i = 0; i < scan.busy_n && off < 90; ++i) {
                off += std::snprintf(busy + off, sizeof(busy) - off, "%s%s",
                                     i == 0 ? "" : ",", scan.busy[i]);
            }
            char wchan[128] = {};
            off = 0;
            for (int i = 0; i < scan.wchan_n && off < 118; ++i) {
                off += std::snprintf(wchan + off, sizeof(wchan) - off, "%s%s:%d",
                                     i == 0 ? "" : ",", scan.wchan[i], scan.wchan_count[i]);
            }

            char line[512];
            const int len = std::snprintf(
                line, sizeof(line),
                "t=%lldms rss=%ldMB peak=%ldMB vm=%ldMB thr=%ld cpu=%llu%% gmain=%llu%% "
                "st=R%d/S%d/D%d flip=%llu pres=%llu gnm=%llu blank=%llu busy=%s w=%s\n",
                static_cast<long long>(ms), rss_kb / 1024, peak_kb / 1024, vm_kb / 1024, threads,
                dcpu * 100 / ticks_per_period, dguest * 100 / ticks_per_period, scan.running,
                scan.sleeping, scan.disk, flips.load(std::memory_order_relaxed),
                presents.load(std::memory_order_relaxed),
                gnm_submits.load(std::memory_order_relaxed),
                blanks.load(std::memory_order_relaxed), busy[0] != '\0' ? busy : "-",
                wchan[0] != '\0' ? wchan : "-");
            if (len > 0) {
                detail::AppendLine(line, len < static_cast<int>(sizeof(line))
                                             ? len
                                             : static_cast<int>(sizeof(line)) - 1);
            }
        }
    }).detach();
}

#else

inline void StartHeartbeat() {}

#endif

} // namespace BachataDiag
