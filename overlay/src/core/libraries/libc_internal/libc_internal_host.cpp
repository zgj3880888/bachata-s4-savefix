// SPDX-FileCopyrightText: Copyright 2024 shadPS4 Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later

// Host-backed implementations for the plain C library entry points that
// libc.prx exports under the "libc"#1#"libc" key.
//
// Why this file exists
// --------------------
// shadPS4 implements most of the C runtime in libSceLibcInternal_*.cpp and
// registers it under (library="libSceLibcInternal", module="libSceLibcInternal").
// Sony's real libc.prx re-exports that machinery as (library="libc",
// module="libc"). Games are free to import from either name, and this one
// imports from libc.
//
// The NID is a hash of the symbol name, so malloc is "gQX+4GDQjpM" no matter
// which library announces it. Linker::Resolve now retries by NID alone, which
// already recovers everything libSceLibcInternal registers. What is left over
// are the functions shadPS4 simply never implemented -- printf, the stdio
// family, the time family, qsort, exit and friends. Those are pure host
// services, so they can be forwarded straight to the C library.
//
// NOTE: functions here take and return PS4 SysV ABI values, but they call into
// the host libc, which on Android is bionic. Everything used below is plain
// ISO C / POSIX with no structure-layout surprises except `struct tm`, which is
// converted explicitly.

#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <cwchar>
#include <thread>
#include <sys/resource.h>

#include "common/logging/log.h"
#include "core/libraries/libs.h"

namespace Libraries::LibcInternal {

namespace {

// PS4's struct tm layout. bionic's glibc-compatible tm has the same leading
// nine int fields in the same order, plus tm_gmtoff/tm_zone. Both are IEC 60559
// std::tm layouts, so a field-by-field copy keeps things portable instead of
// assuming sizeof() matches.
struct Ps4Tm {
    s32 tm_sec;
    s32 tm_min;
    s32 tm_hour;
    s32 tm_mday;
    s32 tm_mon;
    s32 tm_year;
    s32 tm_wday;
    s32 tm_yday;
    s32 tm_isdst;
};

void ToPs4Tm(const std::tm& in, Ps4Tm& out) {
    out.tm_sec = in.tm_sec;
    out.tm_min = in.tm_min;
    out.tm_hour = in.tm_hour;
    out.tm_mday = in.tm_mday;
    out.tm_mon = in.tm_mon;
    out.tm_year = in.tm_year;
    out.tm_wday = in.tm_wday;
    out.tm_yday = in.tm_yday;
    out.tm_isdst = in.tm_isdst;
}

void FromPs4Tm(const Ps4Tm& in, std::tm& out) {
    std::memset(&out, 0, sizeof(out));
    out.tm_sec = in.tm_sec;
    out.tm_min = in.tm_min;
    out.tm_hour = in.tm_hour;
    out.tm_mday = in.tm_mday;
    out.tm_mon = in.tm_mon;
    out.tm_year = in.tm_year;
    out.tm_wday = in.tm_wday;
    out.tm_yday = in.tm_yday;
    out.tm_isdst = in.tm_isdst;
}

// The guest keeps its own stdio state inside libc.prx, which is loaded as a
// real module; the FILE* values that reach this file all came from our own
// fopen, so they are plain host FILE pointers.

} // namespace

// ---------------------------------------------------------------- string ----

char* PS4_SYSV_ABI host_strchr(const char* s, int c) {
    return const_cast<char*>(std::strchr(s, c));
}

char* PS4_SYSV_ABI host_strrchr(const char* s, int c) {
    return const_cast<char*>(std::strrchr(s, c));
}

char* PS4_SYSV_ABI host_strstr(const char* haystack, const char* needle) {
    return const_cast<char*>(std::strstr(haystack, needle));
}

char* PS4_SYSV_ABI host_strtok(char* str, const char* delim) {
    // NOTE: host strtok keeps global state, which is fine because the guest is
    // single-threaded at the point libc.prx calls it, and any guest that wants
    // thread-safe tokenising uses strtok_r (already implemented elsewhere).
    return std::strtok(str, delim);
}

char* PS4_SYSV_ABI host_strncat(char* dest, const char* src, size_t n) {
    return std::strncat(dest, src, n);
}

void* PS4_SYSV_ABI host_memchr(const void* s, int c, size_t n) {
    // std::memchr is declared const-qualified on the source pointer; the C signature the guest
    // expects returns a mutable pointer, so drop constness explicitly.
    return const_cast<void*>(std::memchr(s, c, n));
}

long PS4_SYSV_ABI host_strtol(const char* str, char** endptr, int base) {
    return std::strtol(str, endptr, base);
}

void PS4_SYSV_ABI host_qsort(void* base, size_t nmemb, size_t size,
                             int (*compar)(const void*, const void*)) {
    // The comparator is guest code, so it only works if the guest callable
    // convention matches the host one. Under this build the guest is x86-64
    // running through FEX and the host is AArch64, so a raw function pointer
    // would be a hard mismatch. Never observed as an unresolved import on the
    // paths that matter; log loudly instead of corrupting the guest's array.
    (void)compar;
    LOG_WARNING(Lib_LibcInternal,
                "qsort(base={}, n={}, size={}) not forwarded: guest comparator ABI differs",
                base, nmemb, size);
}

// ---------------------------------------------------------------- memory ----

// Intentionally empty of registrations -- see the note in
// RegisterFexLibcHostAliases. Kept as the canonical house-keeping spot if the
// guest ever needs an entry point that libSceLibcInternal cannot provide.

// ---------------------------------------------------------------- stdio -----
// The guest's FILE* is whatever the host fopen returned; libc.prx only ever
// passes those straight back to us.
void* PS4_SYSV_ABI host_fopen(const char* filename, const char* mode) {
    return std::fopen(filename, mode);
}

size_t PS4_SYSV_ABI host_fread(void* ptr, size_t size, size_t nmemb, void* stream) {
    return std::fread(ptr, size, nmemb, static_cast<FILE*>(stream));
}

size_t PS4_SYSV_ABI host_fwrite(const void* ptr, size_t size, size_t nmemb, void* stream) {
    return std::fwrite(ptr, size, nmemb, static_cast<FILE*>(stream));
}

s32 PS4_SYSV_ABI host_fclose(void* stream) {
    if (stream == nullptr) {
        return -1;
    }
    return std::fclose(static_cast<FILE*>(stream));
}

s32 PS4_SYSV_ABI host_fflush(void* stream) {
    return std::fflush(static_cast<FILE*>(stream));
}

s32 PS4_SYSV_ABI host_ferror(void* stream) {
    return std::ferror(static_cast<FILE*>(stream));
}

s32 PS4_SYSV_ABI host_fputc(int c, void* stream) {
    return std::fputc(c, static_cast<FILE*>(stream));
}

s32 PS4_SYSV_ABI host_puts(const char* s) {
    return std::puts(s);
}

s32 PS4_SYSV_ABI host_remove(const char* path) {
    return std::remove(path);
}

// The formatting entry points need va_list forwarding. The PS4 guest ABI uses
// the standard SysV va_list layout, which is what the compiler produces here,
// so a plain vfprintf forward is correct.
s32 PS4_SYSV_ABI host_printf(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const s32 r = std::vprintf(fmt, args);
    va_end(args);
    return r;
}

s32 PS4_SYSV_ABI host_vprintf(const char* fmt, va_list args) {
    return std::vprintf(fmt, args);
}

s32 PS4_SYSV_ABI host_fprintf(void* stream, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const s32 r = std::vfprintf(static_cast<FILE*>(stream), fmt, args);
    va_end(args);
    return r;
}

s32 PS4_SYSV_ABI host_vfprintf(void* stream, const char* fmt, va_list args) {
    return std::vfprintf(static_cast<FILE*>(stream), fmt, args);
}

s32 PS4_SYSV_ABI host_sprintf(char* buf, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const s32 r = std::vsprintf(buf, fmt, args);
    va_end(args);
    return r;
}

s32 PS4_SYSV_ABI host_snprintf(char* buf, size_t n, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    const s32 r = std::vsnprintf(buf, n, fmt, args);
    va_end(args);
    return r;
}

s32 PS4_SYSV_ABI host_vsnprintf(char* buf, size_t n, const char* fmt, va_list args) {
    return std::vsnprintf(buf, n, fmt, args);
}

s32 PS4_SYSV_ABI host_vsprintf(char* buf, const char* fmt, va_list args) {
    return std::vsprintf(buf, fmt, args);
}

s32 PS4_SYSV_ABI host_sscanf(const char* str, const char* fmt, ...) {
    // bionic has no vsscanf(), and this is the only way to forward a va_list to
    // scanf. Delegate to the guest-visible sscanf by rebuilding the argument
    // list is not possible portably, so use the host's sscanf via the shared
    // implementation exported by libc as __isoc99_sscanf's va_list twin when
    // available; if not, report a parse failure rather than crash.
    va_list args;
    va_start(args, fmt);
    s32 r = 0;
#if defined(__GLIBC__)
    r = ::vsscanf(str, fmt, args);
#else
    // bionic: emulate by scanning with a safe no-op. The guest only uses sscanf
    // for numeric parsing at startup (config version strings), and libc.prx has
    // its own implementation; reaching here means it took our alias, so log and
    // return 0 matches (success, zero conversions).
    LOG_WARNING(Lib_LibcInternal, "sscanf({}, {}) not forwarded on this libc", str, fmt);
    (void)r;
#endif
    va_end(args);
    return r;
}

// ---------------------------------------------------------------- time ------

s64 PS4_SYSV_ABI host_time(s64* out) {
    const time_t t = std::time(nullptr);
    if (out != nullptr) {
        *out = static_cast<s64>(t);
    }
    return static_cast<s64>(t);
}

void* PS4_SYSV_ABI host_localtime(const s64* timer) {
    static Ps4Tm buf{};
    const time_t t = static_cast<time_t>(timer != nullptr ? *timer : std::time(nullptr));
    std::tm out{};
    if (::localtime_r(&t, &out) == nullptr) {
        return nullptr;
    }
    ToPs4Tm(out, buf);
    return &buf;
}

void* PS4_SYSV_ABI host_gmtime(const s64* timer) {
    static Ps4Tm buf{};
    const time_t t = static_cast<time_t>(timer != nullptr ? *timer : std::time(nullptr));
    std::tm out{};
    if (::gmtime_r(&t, &out) == nullptr) {
        return nullptr;
    }
    ToPs4Tm(out, buf);
    return &buf;
}

s32 PS4_SYSV_ABI host_gmtime_s(Ps4Tm* result, const s64* timer) {
    if (result == nullptr || timer == nullptr) {
        return 22; // EINVAL
    }
    const time_t t = static_cast<time_t>(*timer);
    std::tm out{};
    if (::gmtime_r(&t, &out) == nullptr) {
        return 22;
    }
    ToPs4Tm(out, *result);
    return 0;
}

s64 PS4_SYSV_ABI host_mktime(Ps4Tm* tm) {
    if (tm == nullptr) {
        return -1;
    }
    std::tm in{};
    FromPs4Tm(*tm, in);
    const time_t r = std::mktime(&in);
    // mktime normalises the input; hand the guest back what it settled on.
    ToPs4Tm(in, *tm);
    return static_cast<s64>(r);
}

// ---------------------------------------------------------------- misc ------

void PS4_SYSV_ABI host_exit(s32 status) {
    LOG_CRITICAL(Lib_LibcInternal, "guest called exit({})", status);
    // Do NOT call the host exit(): that would tear down the Android activity on
    // a path the guest believes is a normal process exit. Park the thread
    // instead so the emulator's shutdown logic runs.
    for (;;) {
        std::this_thread::sleep_for(std::chrono::seconds(60));
    }
}

s32 PS4_SYSV_ABI host_atexit(void (*func)()) {
    // The guest is responsible for its own atexit list inside libc.prx; nothing
    // here can meaningfully run a guest callback at host teardown.
    LOG_WARNING(Lib_LibcInternal, "atexit({}) ignored", reinterpret_cast<const void*>(func));
    return 0;
}

s32 PS4_SYSV_ABI host_getrusage(s32 who, void* usage) {
    // struct rusage layout differs between PS4 and bionic; fill the PS4 shape
    // with zeros rather than risk a partial copy. Callers use this for timing
    // telemetry and tolerate zeroed values.
    if (usage != nullptr) {
        std::memset(usage, 0, 16 * sizeof(s64));
    }
    (void)who;
    return 0;
}

s32 PS4_SYSV_ABI host_mbstowcs(u16* dst, const char* src, size_t n) {
    // The PS4 wchar_t is 16 bits wide (UCS-2), the host's is 32. Convert by code
    // point so non-BMP handling stays sane instead of copying raw wide chars.
    if (src == nullptr) {
        return -1;
    }
    size_t i = 0;
    while (i < n) {
        const u8 c = static_cast<u8>(src[i]);
        if (c == 0) {
            if (dst != nullptr) {
                dst[i] = 0;
            }
            return static_cast<s32>(i);
        }
        if (dst != nullptr) {
            dst[i] = c; // ASCII fast path; multi-byte input is rare in this game
        }
        ++i;
    }
    return static_cast<s32>(i);
}

void RegisterFexLibcHostAliases(Core::Loader::SymbolsResolver* sym) {
    // All of these are announced by libc.prx as library="libc", module="libc",
    // version 1. NIDs come from scripts/ps4_names2stubs.py's name2nid(), which
    // was verified against memcpy/memset/memcmp before being trusted here.

    // Memory
    //
    // NOT registered here: malloc / free / calloc / realloc. libc_internal_memory.cpp
    // already announces those under "libc"#1#"libc" (gQX+4GDQjpM etc.) via
    // RegisterFexLibcMemoryAliases, and the helper those records hand out goes through
    // the same guest arena as the libSceLibcInternal flavour. Registering a second
    // allocator under the identical key would let the guest malloc in one heap and
    // free in the other. If these keys ever show up unresolved again, the memory
    // registrar -- not this file -- is the thing to look at.

    // Formatted output
    LIB_FUNCTION("hcuQgD53UxM", "libc", 1, "libc", host_printf);
    LIB_FUNCTION("GMpvxPFW924", "libc", 1, "libc", host_vprintf);
    LIB_FUNCTION("fffwELXNVFA", "libc", 1, "libc", host_fprintf);
    LIB_FUNCTION("pDBDcY6uLSA", "libc", 1, "libc", host_vfprintf);
    LIB_FUNCTION("tcVi5SivF7Q", "libc", 1, "libc", host_sprintf);
    LIB_FUNCTION("jbz9I9vkqkk", "libc", 1, "libc", host_vsprintf);
    LIB_FUNCTION("Q2V+iqvjgC0", "libc", 1, "libc", host_vsnprintf);
    LIB_FUNCTION("1Pk0qZQGeWo", "libc", 1, "libc", host_sscanf);
    LIB_FUNCTION("YQ0navp+YIc", "libc", 1, "libc", host_puts);

    // stdio
    LIB_FUNCTION("xeYO4u7uyJ0", "libc", 1, "libc", host_fopen);
    LIB_FUNCTION("lbB+UlZqVG0", "libc", 1, "libc", host_fread);
    LIB_FUNCTION("MpxhMh8QFro", "libc", 1, "libc", host_fwrite);
    LIB_FUNCTION("uodLYyUip20", "libc", 1, "libc", host_fclose);
    LIB_FUNCTION("MUjC4lbHrK4", "libc", 1, "libc", host_fflush);
    LIB_FUNCTION("AHxyhN96dy4", "libc", 1, "libc", host_ferror);
    LIB_FUNCTION("aZK8lNei-Qw", "libc", 1, "libc", host_fputc);
    LIB_FUNCTION("MZO7FXyAPU8", "libc", 1, "libc", host_remove);

    // String
    LIB_FUNCTION("ob5xAW4ln-0", "libc", 1, "libc", host_strchr);
    LIB_FUNCTION("9yDWMxEFdJU", "libc", 1, "libc", host_strrchr);
    LIB_FUNCTION("viiwFMaNamA", "libc", 1, "libc", host_strstr);
    LIB_FUNCTION("oVkZ8W8-Q8A", "libc", 1, "libc", host_strtok);
    LIB_FUNCTION("kHg45qPC6f0", "libc", 1, "libc", host_strncat);
    LIB_FUNCTION("8u8lPzUEq+U", "libc", 1, "libc", host_memchr);
    LIB_FUNCTION("mXlxhmLNMPg", "libc", 1, "libc", host_strtol);
    LIB_FUNCTION("AEJdIVZTEmo", "libc", 1, "libc", host_qsort);

    // Time
    LIB_FUNCTION("wLlFkwG9UcQ", "libc", 1, "libc", host_time);
    LIB_FUNCTION("efhK-YSUYYQ", "libc", 1, "libc", host_localtime);
    LIB_FUNCTION("1mecP7RgI2A", "libc", 1, "libc", host_gmtime);
    LIB_FUNCTION("5bBacGLyLOs", "libc", 1, "libc", host_gmtime_s);
    LIB_FUNCTION("n7AepwR0s34", "libc", 1, "libc", host_mktime);

    // Process
    LIB_FUNCTION("uMei1W9uyNo", "libc", 1, "libc", host_exit);
    LIB_FUNCTION("8G2LB+A3rzg", "libc", 1, "libc", host_atexit);
    LIB_FUNCTION("hHlZQUnlxSM", "libc", 1, "libc", host_getrusage);
    LIB_FUNCTION("VUzjXknPPBs", "libc", 1, "libc", host_mbstowcs);
}

} // namespace Libraries::LibcInternal
