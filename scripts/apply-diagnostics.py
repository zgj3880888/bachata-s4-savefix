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
8. Startup milestones: pad open, system service polls, save mount, audio open.
9. libc_internal -- register the FEX "libc" aliases from RegisterLib (nothing called
   ForceRegisterLib), and give malloc/calloc/realloc/free real implementations.
10. The libc.prx fix proper. libc.prx is a SELF container that exports 1448 symbols as
   "libc"#1#"libc", of which shadPS4 implements four. Two things: Linker::Resolve now retries
   by NID alone (same NID + same type == same function), recovering ~71 symbols that already
   exist under libSceLibcInternal/libSceFios2/libSceAvPlayer/libSceRtc; and libc_internal_host.cpp
   forwards the plain C entry points shadPS4 never implemented (printf, stdio, time, qsort)
   to the host C library. ~111 of the 245 remaining keys.

Usage: scripts/apply-diagnostics.py <core-dir>
"""

import pathlib
import re
import sys

MARK = b"BachataDiag::"


def read(path):
    p = pathlib.Path(path)
    if not p.is_file():
        sys.exit(f"missing {path}")
    return p


def edit(path, pairs, marker=None, note="", already_marker=None):
    """Apply (old, new) byte pairs to `path`, tolerating either line ending.

    `marker` is the text that only exists *after* this edit; seeing it means "already done".
    `already_marker` is the same idea for a change an earlier build may have made. Groups 9
    and 10 both write into RegisterLib(), so on a tree that was patched before, group 9's own
    marker can be absent while its intent is already satisfied. Treating that as a hard error
    would make group 10 unreachable, so those callers pass `already_marker` too.
    """
    p = read(path)
    data = p.read_bytes()
    if marker is not None and marker in data:
        print(f"already applied {path} {note}")
        return
    if already_marker is not None and already_marker in data:
        print(f"already applied {path} {note} (superseded by later group)")
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

# ---------------------------------------------------------------------------------------------
# 7b. call tail -- the last N HLE calls, in order
#
# The milestone counters are cumulative and unordered, so they cannot say where a guest that went
# quiet stopped. This records every HLE call through the typed adapter (i.e. every *implemented*
# function, not just the unresolved ones) into a 32-slot ring, which the heartbeat prints. A ring
# that has stopped changing between two heartbeats means the guest is no longer entering HLE at
# all, and its last entries name the call it is sitting behind.
# ---------------------------------------------------------------------------------------------
HCA_TYPED_OLD = b"""    HleCallResult Invoke(HleCallFrame& frame) const override {
        if constexpr ((!IsSupportedArgument<Args> || ...) || !IsSupportedReturn<Return>) {
"""

HCA_TYPED_NEW = b"""    HleCallResult Invoke(HleCallFrame& frame) const override {
        // Bachata: one relaxed store plus a counter, so recording every implemented HLE call is
        // cheap enough to leave on. See BachataDiag::NoteHleCall.
        BachataDiag::NoteHleCall(Name());
        if constexpr ((!IsSupportedArgument<Args> || ...) || !IsSupportedReturn<Return>) {
"""

# ---------------------------------------------------------------------------------------------
# 7d. let a 16-byte vector argument cross the FEX bridge
#
# libSceLibcInternal's snprintf is declared as (char*, u64, VA_ARGS), and common/va_ctx.h expands
# VA_ARGS into the x86-64 argument registers spelled out one by one: rdi,rsi,rdx,rcx,r8,r9,
# overflow_arg_area, then eight __m128. Off x86, __m128 is a typedef for __int128.
#
# Those eight are therefore 16 bytes wide. Nothing in this adapter describes a 16-byte argument, so
# every predicate below evaluated false, the if constexpr in Invoke() compiled the ENOTSUP branch in
# permanently, and *every* call to a function that spells its varargs out this way returned -95
# without formatting a single byte. The r23 diagnostic dump shows exactly two such calls in a 1.8
# second run, and the second one is the last thing that happened before the access violation:
#
#   snprintf   libSceLibcInternal  eLdDw6l0-bU   failed: 95
#   vsnprintf  libc                Q2V+iqvjgC0   failed: 95
#
# The general-purpose half of that parameter list already decodes correctly: CallCursor walks
# rdi,rsi,rdx,rcx,r8,r9 and then the guest stack, which is exactly what the compiler would have
# loaded into the callee's rdi..r9. Only the xmm half was missing. Adding it makes the whole
# printf family work through the existing va_ctx.h machinery instead of reinventing formatting.
HCA_WIDE_OLD = b"""template <typename T>
inline constexpr bool IsSupportedArgument = FitsIntegerRegister<T> || IsVectorArgument<T>;
"""

HCA_WIDE_NEW = b"""// Bachata: __m128 is __int128 off x86 (see common/va_ctx.h), so a vector argument that wide
// arrives in one xmm register holding both halves. Without this, every function that spells its
// varargs out as parameters -- the whole printf family -- fails with ENOTSUP on every call.
template <typename T>
inline constexpr bool IsWideVectorArgument = [] {
#if defined(__SIZEOF_INT128__)
    return std::is_same_v<std::remove_cv_t<T>, __int128>;
#else
    return false;
#endif
}();

template <typename T>
inline constexpr bool IsSupportedArgument =
    FitsIntegerRegister<T> || IsVectorArgument<T> || IsWideVectorArgument<T>;
"""

HCA_CURSOR_OLD = b"""    std::optional<u64> NextVector() {
        if (vector_index < 8) {
            return frame.xmm[vector_index++][0];
        }
        return NextStackSlot();
    }
"""

HCA_CURSOR_NEW = b"""    std::optional<u64> NextVector() {
        if (vector_index < 8) {
            return frame.xmm[vector_index++][0];
        }
        return NextStackSlot();
    }

    // A 16-byte vector argument occupies a whole xmm register: low half first, then high half.
    std::optional<std::array<u64, 2>> NextVectorWide() {
        if (vector_index < 8) {
            return frame.xmm[vector_index++];
        }
        return std::nullopt;
    }
"""

HCA_DECODE_OLD = b"""std::optional<T> DecodeArgument(CallCursor& cursor) {
    if constexpr (IsVectorArgument<T>) {
"""

HCA_DECODE_NEW = b"""std::optional<T> DecodeArgument(CallCursor& cursor) {
    if constexpr (IsWideVectorArgument<T>) {
        const auto value = cursor.NextVectorWide();
        if (!value) return std::nullopt;
        T result{};
        std::memcpy(&result, value->data(), sizeof(result));
        return result;
    } else if constexpr (IsVectorArgument<T>) {
"""

# ---------------------------------------------------------------------------------------------
# 7e. print the HLE call ring when the guest dies
#
# The heartbeat samples every five seconds. This title now reaches its access violation 1.8 seconds
# in, so the r23 report carried no heartbeat at all and therefore no "tail=" field: the one piece of
# information that says which calls the guest was making when it died. The ring already exists; it
# just needs to be drained on the fatal path as well.
SIG_INCLUDE_OLD = b"""#include <fmt/format.h>
#include "common/arch.h"
"""

SIG_INCLUDE_NEW = b"""#include <fmt/format.h>
#include "common/arch.h"
#include "common/bachata_diag.h"
"""

SIG_TAIL_OLD = b"""            if (::Core::Fex::BachataQueryGuestRipSyscall(&guest_rip, &guest_rax)) {
                LOG_CRITICAL(Debug, "FEX guest state at fault: rip={:#x} rax={:#x}", guest_rip,
                             guest_rax);
            }
#endif
"""

SIG_TAIL_NEW = b"""            if (::Core::Fex::BachataQueryGuestRipSyscall(&guest_rip, &guest_rax)) {
                LOG_CRITICAL(Debug, "FEX guest state at fault: rip={:#x} rax={:#x}", guest_rip,
                             guest_rax);
            }
            {
                // Bachata: drain the last-N HLE call ring here too, because a run that dies
                // faster than one heartbeat period otherwise reports nothing at all. This has
                // to stay inside the FEX guard: without it bachata_diag.h has no detail::
                // namespace at all, only the no-op stubs.
                char hle_tail[640];
                BachataDiag::detail::FormatCallTail(hle_tail, static_cast<int>(sizeof(hle_tail)));
                LOG_CRITICAL(Debug, "HLE call tail at fault: {}", hle_tail);
            }
#endif
"""

# ---------------------------------------------------------------------------------------------
# 7c. flush the log file after every message
#
# LogFileSink writes through spdlog::details::file_helper_, i.e. a fully buffered stdio stream.
# Two runs in a row ended at a size that was an exact multiple of 4096 and cut mid-line: the on-disk
# prefix is everything stdio had already pushed out, and the interesting tail -- what the core was
# doing when the log was pulled -- never left the buffer. UnbufferLogs() does not help, because it
# only touches stdout/stderr, not this stream.
# ---------------------------------------------------------------------------------------------
LOGSINK_OLD = b"""        if (session_file_helper_.filename().empty()) {
            // always log in the main file, it shouldnt be too big
            main_file_helper_.write(formatted);
        } else if (_current_size < _size_limit || msg.log_level == spdlog::level::critical) {
            session_file_helper_.write(formatted);
            _current_size += formatted.size();
        }
"""

LOGSINK_NEW = b"""        if (session_file_helper_.filename().empty()) {
            // always log in the main file, it shouldnt be too big
            main_file_helper_.write(formatted);
            main_file_helper_.flush();
        } else if (_current_size < _size_limit || msg.log_level == spdlog::level::critical) {
            session_file_helper_.write(formatted);
            _current_size += formatted.size();
            // Bachata: file_helper_ is a fully buffered stdio stream, so whatever is still in the
            // buffer never reaches disk if the run is killed -- or if the log is simply pulled
            // while the core is still alive, which is what happened to the last two runs. Flushing
            // per message costs one syscall per line and keeps every complete line on disk.
            session_file_helper_.flush();
        }
"""

# ---------------------------------------------------------------------------------------------
# 7f. make the log survive the run
#
# Per-message flush is useless if the message never reaches the sink. The default chain is
#     logger -> dup_filter_sink_mt -> async_sink -> [console, file]
# and async_sink hands records to a background thread through a queue. A guest that dies on a
# signal never drains that queue, so the queued part -- the tail worth reading -- is gone. Switch()
# then reopened the session file with truncation on every attempt, and the app retries the core
# several times per launch (the r23 log shows arm64-025 twice followed by the dispatcher four
# times), so each attempt erased the one before it. That is why six rounds of diagnosis only ever
# saw the first 4 KB of a run and why every conclusion drawn past that point was guesswork.
# Log synchronously, flush on every record, and never truncate.
# ---------------------------------------------------------------------------------------------
LOG_SINKS_OLD = b"""    std::initializer_list<spdlog::sink_ptr> async_sink{std::make_shared<spdlog::sinks::async_sink>(
        spdlog::sinks::async_sink::config{.sinks = sinks})};

    std::initializer_list<spdlog::sink_ptr> dup_filter{
        std::make_shared<spdlog::sinks::dup_filter_sink_mt>(
            std::chrono::milliseconds(EmulatorSettings.GetLogMaxSkipDuration()),
            EmulatorSettings.IsLogSync() ? sinks : async_sink)};

    for (auto& logger : ALL_LOGGERS) {
        logger->sinks() = EmulatorSettings.IsLogSkipDuplicate()
                              ? dup_filter
                              : (EmulatorSettings.IsLogSync() ? sinks : async_sink);
    }
"""

LOG_SINKS_NEW = b"""    // Bachata: always synchronous. async_sink is cheaper per record, but it is a queue drained by
    // a background thread and a crashing guest never lets it drain.
    for (auto& logger : ALL_LOGGERS) {
        logger->sinks() = sinks;
        logger->flush_on(spdlog::level::trace);
    }
"""

LOG_FLUSH_OLD = b"""void UpdateLogFlushLevel(std::string_view log_flush_level) {
    if (!log_flush_level.empty()) {
        for (auto& logger : ALL_LOGGERS) {
            logger->flush_on(spdlog::level_from_str(log_flush_level.data()));
        }
    }
}
"""

LOG_FLUSH_NEW = b"""void UpdateLogFlushLevel(std::string_view log_flush_level) {
    // Bachata: ignore the configured level. Switch() calls UpdateSinks() and then this, so
    // anything weaker than trace would immediately undo the per-record flush set above.
    static_cast<void>(log_flush_level);
    for (auto& logger : ALL_LOGGERS) {
        logger->flush_on(spdlog::level::trace);
    }
}
"""

LOG_SWITCH_OLD = b"""    g_shad_file_sink->session_file_helper_.open(
        (GetUserPath(Common::FS::PathType::LogDir) / game_filename).string(),
        !(append_log || EmulatorSettings.IsLogAppend()));
"""

LOG_SWITCH_NEW = b"""    // Bachata: never truncate. The app retries the core several times per launch and every
    // attempt used to erase the previous one, which is how six runs produced a single 4 KB log.
    static_cast<void>(append_log);
    g_shad_file_sink->session_file_helper_.open(
        (GetUserPath(Common::FS::PathType::LogDir) / game_filename).string(), false);
    LOG_INFO(Config, "=== Bachata: session start (append, sync) ===");
"""

# ---------------------------------------------------------------------------------------------
# 7g. tiling pipeline cache key
#
# Upstream 0.21 keys the tiling pipeline cache on tile_mode + num_bits + num_samples + is_tiler,
# but array_mode is what actually picks the shader. AmdGpu::IsMacroTiled(array_mode) selects
# between the macro and micro modules outright, and AmdGpu::GetMicroTileThickness(array_mode)
# feeds MICRO_TILE_THICKNESS (1, 4 or 8). So two images agreeing on tile_mode/bits/samples but
# disagreeing on array_mode collided in the cache and the first one created decided how the other
# was detiled: a micro image run through the macro module, or a thick image detiled as thin. That
# is a shifted / blocky / black texture, not a crash, which matches the symptom instead of the
# startup wall we have been chasing.
#
# The port tree's own key (tile_mode * NUM_BPPS + bpp index) is coarser still, so this is not a
# regression to restore -- it is a hole both trees have and 0.21 narrows without closing.
# ---------------------------------------------------------------------------------------------
TM_KEY_OLD = b"""    struct TilingKey {
        AmdGpu::TileMode tile_mode;
        u32 num_bits;
        u32 num_samples;
        bool is_tiler;

        bool operator==(const TilingKey&) const = default;

        struct Hash {
            size_t operator()(const TilingKey& key) const {
                size_t hash = 0;
                boost::hash_combine(hash, key.tile_mode);
                boost::hash_combine(hash, key.num_bits);
"""

TM_KEY_NEW = b"""    struct TilingKey {
        AmdGpu::TileMode tile_mode;
        // Bachata: array_mode decides which module gets bound, not just a constant inside it.
        AmdGpu::ArrayMode array_mode;
        u32 num_bits;
        u32 num_samples;
        bool is_tiler;

        bool operator==(const TilingKey&) const = default;

        struct Hash {
            size_t operator()(const TilingKey& key) const {
                size_t hash = 0;
                boost::hash_combine(hash, key.tile_mode);
                boost::hash_combine(hash, key.array_mode);
                boost::hash_combine(hash, key.num_bits);
"""

TM_CPP_OLD = b"""    const TilingKey key{
        .tile_mode = info.tile_mode,
        .num_bits = info.num_bits,
        .num_samples = info.num_samples,
        .is_tiler = is_tiler,
    };
"""

TM_CPP_NEW = b"""    const TilingKey key{
        .tile_mode = info.tile_mode,
        .array_mode = info.array_mode,
        .num_bits = info.num_bits,
        .num_samples = info.num_samples,
        .is_tiler = is_tiler,
    };
"""

TM_LOG_OLD = b"""    LOG_INFO(Render_Vulkan, "Creating tiling pipeline {}", module_name);
"""

TM_LOG_NEW = b"""    // Bachata: array_mode and num_samples are part of the cache key now, so print them next to
    // the module name -- a log line is the only place that shows which variant an image got.
    LOG_INFO(Render_Vulkan, "Creating tiling pipeline {} array_mode={} samples={} macro={}",
             module_name, u32(info.array_mode), info.num_samples, is_macro);
"""

# ---------------------------------------------------------------------------------------------
# 7h. Ngs2 -- the allocator callbacks are guest function pointers
#
# sceNgs2SystemCreateWithAllocator / sceNgs2RackCreateWithAllocator both take a callback pair and
# invoke them directly. On an x86-64 host that happens to be correct, because the guest *is* x86.
# Under FEX the guest is x86 but the host is AArch64, so a direct call enters x86-64 bytes with
# AArch64 semantics: it either faults or returns garbage. Either way the buffer comes back empty,
# ValidateBuffer rejects it, and the caller retries -- which is exactly the six-call loop the
# heartbeat shows (pthread_self / rwlock_* / sceSysmoduleLoadModule / SystemCreateWithAllocator)
# with gnm=0, flip=0 for minutes on end.
#
# The fix routes every callback through Linker::RunGuestFunction, and when a callback still yields
# nothing usable it degrades to host memory instead of failing. The 0.1.6 lineage (what S4+ ships)
# never had this problem because the whole module just returned ORBIS_OK.
# ---------------------------------------------------------------------------------------------
NGS2_INCLUDE_OLD = b"""#include "core/libraries/ngs2/ngs2_submixer.h"
"""

NGS2_INCLUDE_NEW = b"""#include "core/libraries/ngs2/ngs2_submixer.h"
#ifdef SHADPS4_ENABLE_FEX_GUEST_CPU
#include "core/guest_cpu/guest_callback.h"
#endif
"""

NGS2_OPTION_OLD = b"""    if (option) {
        if (option->size != sizeof(*option)) {
            return ORBIS_NGS2_ERROR_INVALID_OPTION_SIZE;
        }

        result = *option;
    }

    if (!ValidGrain(result.maxGrainSamples)) {
        return ORBIS_NGS2_ERROR_INVALID_MAX_GRAIN_SAMPLES;
    }

    if (!ValidGrain(result.numGrainSamples)) {
        return ORBIS_NGS2_ERROR_INVALID_NUM_GRAIN_SAMPLES;
    }

    if (!ValidSampleRate(result.sampleRate)) {
        return ORBIS_NGS2_ERROR_INVALID_SAMPLE_RATE;
    }
"""

NGS2_OPTION_NEW = b"""    if (option) {
        // Bachata: the guest decides sizeof(OrbisNgs2SystemOption) from its own SDK headers, so a
        // mismatch here used to turn into a silent retry loop. Read min(size, ours) instead and
        // say what arrived.
        if (option->size == 0 || option->size > 4096) {
            LOG_ERROR(Lib_Ngs2, "sceNgs2SystemOption size {} is unusable", option->size);
            return ORBIS_NGS2_ERROR_INVALID_OPTION_SIZE;
        }

        if (option->size != sizeof(*option)) {
            LOG_WARNING(Lib_Ngs2, "sceNgs2SystemOption size {} != {}; reading a prefix",
                        option->size, sizeof(*option));
        }

        std::memcpy(&result, option, std::min(option->size, sizeof(result)));
    }

    LOG_INFO(Lib_Ngs2, "system option grain={}/{} rate={} flags={:#x}",
             result.maxGrainSamples, result.numGrainSamples, result.sampleRate, result.flags);

    // Bachata: never reject the option. A rejection is invisible to the guest except as "audio
    // init failed", and it retries forever instead of starting the game.
    if (!ValidGrain(result.maxGrainSamples)) {
        LOG_WARNING(Lib_Ngs2, "maxGrainSamples {} invalid; clamping to 512",
                    result.maxGrainSamples);
        result.maxGrainSamples = 512;
    }

    if (result.numGrainSamples == 0) {
        result.numGrainSamples = result.maxGrainSamples;
    } else if (!ValidGrain(result.numGrainSamples)) {
        LOG_WARNING(Lib_Ngs2, "numGrainSamples {} invalid; clamping to {}",
                    result.numGrainSamples, result.maxGrainSamples);
        result.numGrainSamples = result.maxGrainSamples;
    }

    if (!ValidSampleRate(result.sampleRate)) {
        LOG_WARNING(Lib_Ngs2, "sampleRate {} unsupported; clamping to 48000", result.sampleRate);
        result.sampleRate = 48000;
    }
"""

NGS2_HELPERS_OLD = b"""void QueryBuffer(OrbisNgs2ContextBufferInfo& buffer, size_t size) {
    buffer.hostBuffer = nullptr;
    buffer.hostBufferSize = size;
    std::ranges::fill(buffer.reserved, 0);
}
"""

NGS2_HELPERS_NEW = b"""void QueryBuffer(OrbisNgs2ContextBufferInfo& buffer, size_t size) {
    buffer.hostBuffer = nullptr;
    buffer.hostBufferSize = size;
    std::ranges::fill(buffer.reserved, 0);
}

// Bachata: sceNgs2 hands us guest function pointers. Calling them directly is only valid when the
// guest is the host (x86-64); under FEX the call has to go through the bridge, and a callback that
// yields no usable buffer has to degrade to host memory rather than fail the whole audio init.
namespace {

class HostBufferPool final {
public:
    void* Acquire(size_t size) {
        std::scoped_lock lock{mutex};
        auto block = std::make_unique<u8[]>(size);
        void* raw = block.get();
        blocks.push_back(std::move(block));
        return raw;
    }

private:
    std::mutex mutex;
    std::vector<std::unique_ptr<u8[]>> blocks;
};

HostBufferPool& BufferPool() {
    static HostBufferPool pool;
    return pool;
}

template <typename Handler>
s32 CallBufferHandler(Handler handler, OrbisNgs2ContextBufferInfo* info, const char* label) {
    if (handler == nullptr) {
        return ORBIS_OK;
    }
#ifdef SHADPS4_ENABLE_FEX_GUEST_CPU
    if (Core::GuestCpu::IsGuestFunctionAddress(reinterpret_cast<const void*>(handler))) {
        const auto status = static_cast<s32>(Core::GuestCpu::RunGuestFunctionOrAbort(
            reinterpret_cast<const void*>(handler), label, info));
        LOG_INFO(Lib_Ngs2, "{}: guest callback -> {} buffer={} size={}", label, status,
                 info->hostBuffer, info->hostBufferSize);
        return status;
    }
#endif
    const auto status = handler(info);
    LOG_INFO(Lib_Ngs2, "{}: callback -> {} buffer={} size={}", label, status, info->hostBuffer,
             info->hostBufferSize);
    return status;
}

OrbisNgs2ContextBufferInfo EnsureUsableBuffer(OrbisNgs2ContextBufferInfo buffer, size_t required,
                                              const char* label, bool* host_owned) {
    *host_owned = false;
    if (buffer.hostBuffer != nullptr && buffer.hostBufferSize >= required) {
        return buffer;
    }

    LOG_WARNING(Lib_Ngs2,
                "{}: allocator gave no usable buffer (buffer={} size={} required={}); using host "
                "memory",
                label, buffer.hostBuffer, buffer.hostBufferSize, required);
    buffer.hostBuffer = BufferPool().Acquire(required);
    buffer.hostBufferSize = required;
    *host_owned = true;
    return buffer;
}

} // namespace
"""

NGS2_RELEASE_OLD = b"""    void Release(OrbisNgs2ContextBufferInfo* outInfo = nullptr) {
        if (freeHandler) {
            freeHandler(&buffer);
        }
"""

NGS2_RELEASE_NEW = b"""    void Release(OrbisNgs2ContextBufferInfo* outInfo = nullptr) {
        if (freeHandler) {
            // Bachata: same guest-pointer rule as the allocator.
            CallBufferHandler(freeHandler, &buffer, "buffer release");
        }
"""

NGS2_SYSCREATE_OLD = b"""s32 SystemCreateWithAllocator(const OrbisNgs2SystemOption* option,
                              const OrbisNgs2BufferAllocator* allocator,
                              OrbisNgs2Handle* outHandle) {
    if (!allocator || !allocator->allocHandler) {
        return ORBIS_NGS2_ERROR_INVALID_BUFFER_ALLOCATOR;
    }

    if (!outHandle) {
        return ORBIS_NGS2_ERROR_INVALID_OUT_ADDRESS;
    }

    const auto callbacks = *allocator;
    OrbisNgs2SystemOption normalized;
    auto result = ReadSystemOption(option, normalized);
    if (result < 0) {
        return result;
    }

    OrbisNgs2ContextBufferInfo buffer{};
    QueryBuffer(buffer, SystemBufferSize(normalized));
    buffer.userData = callbacks.userData;
    result = callbacks.allocHandler(&buffer);
    if (result < 0) {
        return result;
    }

    result = ReadSystemOption(option, normalized);
    if (result >= 0) {
        result = CreateSystem(normalized, buffer, callbacks.freeHandler, outHandle);
    }

    if (result < 0 && callbacks.freeHandler) {
        callbacks.freeHandler(&buffer);
    }

    return result;
}

s32 SystemDestroy("""

NGS2_SYSCREATE_NEW = b"""s32 SystemCreateWithAllocator(const OrbisNgs2SystemOption* option,
                              const OrbisNgs2BufferAllocator* allocator,
                              OrbisNgs2Handle* outHandle) {
    if (!allocator || !allocator->allocHandler) {
        LOG_ERROR(Lib_Ngs2, "SystemCreateWithAllocator: no allocator");
        return ORBIS_NGS2_ERROR_INVALID_BUFFER_ALLOCATOR;
    }

    if (!outHandle) {
        LOG_ERROR(Lib_Ngs2, "SystemCreateWithAllocator: no outHandle");
        return ORBIS_NGS2_ERROR_INVALID_OUT_ADDRESS;
    }

    const auto callbacks = *allocator;
    OrbisNgs2SystemOption normalized;
    auto result = ReadSystemOption(option, normalized);
    if (result < 0) {
        LOG_ERROR(Lib_Ngs2, "SystemCreateWithAllocator: option rejected {}", result);
        return result;
    }

    OrbisNgs2ContextBufferInfo buffer{};
    QueryBuffer(buffer, SystemBufferSize(normalized));
    buffer.userData = callbacks.userData;
    result = CallBufferHandler(callbacks.allocHandler, &buffer, "SystemCreateWithAllocator");
    if (result < 0) {
        LOG_ERROR(Lib_Ngs2, "SystemCreateWithAllocator: allocator returned {}", result);
        return result;
    }

    bool host_owned = false;
    buffer = EnsureUsableBuffer(buffer, SystemBufferSize(normalized), "SystemCreateWithAllocator",
                               &host_owned);

    result = ReadSystemOption(option, normalized);
    if (result >= 0) {
        result = CreateSystem(normalized, buffer, host_owned ? nullptr : callbacks.freeHandler,
                              outHandle);
    }

    LOG_INFO(Lib_Ngs2, "SystemCreateWithAllocator -> {} handle={} host_buffer={}", result,
             outHandle != nullptr ? *outHandle : 0, host_owned);

    if (result < 0 && !host_owned && callbacks.freeHandler) {
        CallBufferHandler(callbacks.freeHandler, &buffer, "SystemCreateWithAllocator free");
    }

    return result;
}

s32 SystemDestroy("""

NGS2_RACKCREATE_OLD = b"""    const auto callbacks = *allocator;
    OrbisNgs2ContextBufferInfo buffer{};
    QueryBuffer(buffer, RackBufferSize(config));
    buffer.userData = callbacks.userData;
    result = callbacks.allocHandler(&buffer);
    if (result < 0) {
        return result;
    }

    result = ReadRackOption(rackId, option, config);
    if (result >= 0) {
        auto system = LockedHandle{systemHandle, HandleType::System};
        result = system ? CreateRack(system.entry.system, config, buffer, callbacks.freeHandler,
                                     outHandle)
                        : ORBIS_NGS2_ERROR_INVALID_SYSTEM_HANDLE;
    }

    if (result < 0 && callbacks.freeHandler) {
        callbacks.freeHandler(&buffer);
    }

    return result;
}
"""

NGS2_RACKCREATE_NEW = b"""    const auto callbacks = *allocator;
    OrbisNgs2ContextBufferInfo buffer{};
    QueryBuffer(buffer, RackBufferSize(config));
    buffer.userData = callbacks.userData;
    result = CallBufferHandler(callbacks.allocHandler, &buffer, "RackCreateWithAllocator");
    if (result < 0) {
        LOG_ERROR(Lib_Ngs2, "RackCreateWithAllocator: allocator returned {}", result);
        return result;
    }

    const auto required = RackBufferSize(config);
    bool host_owned = false;
    buffer = EnsureUsableBuffer(buffer, required, "RackCreateWithAllocator", &host_owned);

    result = ReadRackOption(rackId, option, config);
    if (result >= 0) {
        auto system = LockedHandle{systemHandle, HandleType::System};
        result = system ? CreateRack(system.entry.system, config, buffer,
                                     host_owned ? nullptr : callbacks.freeHandler, outHandle)
                        : ORBIS_NGS2_ERROR_INVALID_SYSTEM_HANDLE;
    }

    LOG_INFO(Lib_Ngs2, "RackCreateWithAllocator id={:#x} -> {} handle={} host_buffer={}", rackId,
             result, outHandle != nullptr ? *outHandle : 0, host_owned);

    if (result < 0 && !host_owned && callbacks.freeHandler) {
        CallBufferHandler(callbacks.freeHandler, &buffer, "RackCreateWithAllocator free");
    }

    return result;
}
"""

# ---------------------------------------------------------------------------------------------
# 7. startup milestone counters
#
# The vblank loop keeps drawing blank frames at 60 Hz while the guest never submits, so the log
# cannot be ordered against wall time. Counting the milestones tells which of them the guest ever
# reached, and the heartbeat can then say whether it is blocked waiting for one of them.
# ---------------------------------------------------------------------------------------------
PAD_INCLUDE_OLD = b"""#include "common/elf_info.h"
#include "common/logging/log.h"
#include "common/singleton.h"
"""

PAD_INCLUDE_NEW = b"""#include "common/bachata_diag.h"
#include "common/elf_info.h"
#include "common/logging/log.h"
#include "common/singleton.h"
"""

PAD_OPEN_OLD = b"""int PS4_SYSV_ABI scePadOpen(Libraries::UserService::OrbisUserServiceUserId userId, s32 type,
                            s32 index, const OrbisPadOpenParam* pParam) {
    if (!g_initialized) {
"""

PAD_OPEN_NEW = b"""int PS4_SYSV_ABI scePadOpen(Libraries::UserService::OrbisUserServiceUserId userId, s32 type,
                            s32 index, const OrbisPadOpenParam* pParam) {
    // A game with no pad handle yet never reports input to its main loop.
    BachataDiag::pad_opens.fetch_add(1, std::memory_order_relaxed);
    if (!g_initialized) {
"""

SYS_INCLUDE_OLD = b"""#include "common/elf_info.h"
#include "common/singleton.h"
"""

SYS_INCLUDE_NEW = b"""#include "common/bachata_diag.h"
#include "common/elf_info.h"
#include "common/singleton.h"
"""

SYS_STATUS_OLD = b"""s32 PS4_SYSV_ABI sceSystemServiceGetStatus(OrbisSystemServiceStatus* status) {
    LOG_TRACE(Lib_SystemService, "called");
"""

SYS_STATUS_NEW = b"""s32 PS4_SYSV_ABI sceSystemServiceGetStatus(OrbisSystemServiceStatus* status) {
    BachataDiag::sys_getstatus.fetch_add(1, std::memory_order_relaxed);
    LOG_TRACE(Lib_SystemService, "called");
"""

SYS_RECV_OLD = b"""s32 PS4_SYSV_ABI sceSystemServiceReceiveEvent(OrbisSystemServiceEvent* event) {
    LOG_TRACE(Lib_SystemService, "called");
"""

SYS_RECV_NEW = b"""s32 PS4_SYSV_ABI sceSystemServiceReceiveEvent(OrbisSystemServiceEvent* event) {
    // Polled in a loop by startup code that waits for the system to report readiness.
    BachataDiag::sys_recvevent.fetch_add(1, std::memory_order_relaxed);
    LOG_TRACE(Lib_SystemService, "called");
"""

SYS_EVENT_OLD = b"""    *event = g_event_queue.front();
    g_event_queue.pop();
"""

SYS_EVENT_NEW = b"""    BachataDiag::sys_event.fetch_add(1, std::memory_order_relaxed);
    *event = g_event_queue.front();
    g_event_queue.pop();
"""

SYS_PARAM_OLD = b"""    LOG_DEBUG(Lib_SystemService, "called param_id {}", u32(param_id));
"""

SYS_PARAM_NEW = b"""    BachataDiag::sys_paramgetint.fetch_add(1, std::memory_order_relaxed);
    LOG_DEBUG(Lib_SystemService, "called param_id {}", u32(param_id));
"""

SAVE_INCLUDE_OLD = b"""#include "common/assert.h"
#include "common/cstring.h"
"""

SAVE_INCLUDE_NEW = b"""#include "common/assert.h"
#include "common/bachata_diag.h"
#include "common/cstring.h"
"""

SAVE_MOUNT_OLD = b"""Error PS4_SYSV_ABI sceSaveDataMount(const OrbisSaveDataMount* mount,
                                    OrbisSaveDataMountResult* mount_result) {
    if (!g_initialized) {
"""

SAVE_MOUNT_NEW = b"""Error PS4_SYSV_ABI sceSaveDataMount(const OrbisSaveDataMount* mount,
                                    OrbisSaveDataMountResult* mount_result) {
    BachataDiag::save_mounts.fetch_add(1, std::memory_order_relaxed);
    if (!g_initialized) {
"""

AUDIO_INCLUDE_OLD = b"""#include <fmt/format.h>
#include <magic_enum/magic_enum.hpp>
"""

AUDIO_INCLUDE_NEW = b"""#include <fmt/format.h>
#include <magic_enum/magic_enum.hpp>
#include "common/bachata_diag.h"
"""

AUDIO_OPEN_OLD = b"""                                 OrbisAudioOutParamExtendedInformation param_type) {
    LOG_INFO(Lib_AudioOut,
"""

AUDIO_OPEN_NEW = b"""                                 OrbisAudioOutParamExtendedInformation param_type) {
    BachataDiag::audio_opens.fetch_add(1, std::memory_order_relaxed);
    LOG_INFO(Lib_AudioOut,
"""

# ---------------------------------------------------------------------------------------------
# 9. libc_internal: actually wire up the FEX "libc" aliases
#
# Root cause found on 2026-11-13 from a clean r19 capture: the guest's very first
# allocation returned -38. linker.cpp:873 hands out UnsupportedHleCallAdapter for anything
# that is not in the HLE table, and that adapter returns HleCallFailure{ENOSYS, Name()}.
# `malloc`, `free`, `strlen`, `memcpy_s` were all in that state -- 753 such warnings in one
# 120 s run, and the guest's startup stalls right after the last CreateStack because every
# allocation afterwards yields an unusable pointer.
#
# The aliases existed in the tree (RegisterFexLibcMemoryAliases / ...StrAliases / ...CxaAliases)
# but were only reachable through ForceRegisterLib(), and nothing ever called that -- the sole
# registration entry point in use is linker.cpp:489 -> LibcInternal::RegisterLib(). So the fix
# is two-part: register the aliases from RegisterLib (where they are guaranteed to land before
# any guest module is relocated), and give malloc/calloc/realloc/free real implementations.
# ---------------------------------------------------------------------------------------------

# RegisterLib() is no longer edited with a byte anchor -- arm_libc_internal_cpp() splices into
# the function body by brace matching. Two shapes exist in the wild and the obvious idempotency
# marker is ambiguous, which is what broke the first r21 build: see the note beside FEX_BLOCK.

LIBC_MEM_ANCHOR_OLD = b"""void RegisterFexLibcMemoryAliases(Core::Loader::SymbolsResolver* sym) {
    LIB_FUNCTION("Q3VBxCXhUHs", "libc", 1, "libc", internal_memcpy);
    LIB_FUNCTION("8zTFvBIAIN8", "libc", 1, "libc", internal_memset);
    LIB_FUNCTION("DfivPArhucg", "libc", 1, "libc", internal_memcmp);
}"""

LIBC_MEM_ANCHOR_NEW = b"""void* PS4_SYSV_ABI bachata_fex_malloc(size_t size) {
    return std::malloc(size == 0 ? 1 : size);
}

void* PS4_SYSV_ABI bachata_fex_calloc(size_t nelem, size_t size) {
    return std::calloc(nelem == 0 ? 1 : nelem, size == 0 ? 1 : size);
}

void* PS4_SYSV_ABI bachata_fex_realloc(void* ptr, size_t size) {
    return std::realloc(ptr, size == 0 ? 1 : size);
}

void PS4_SYSV_ABI bachata_fex_free(void* ptr) {
    std::free(ptr);
}

void RegisterFexLibcMemoryAliases(Core::Loader::SymbolsResolver* sym) {
    LIB_FUNCTION("Q3VBxCXhUHs", "libc", 1, "libc", internal_memcpy);
    LIB_FUNCTION("8zTFvBIAIN8", "libc", 1, "libc", internal_memset);
    LIB_FUNCTION("DfivPArhucg", "libc", 1, "libc", internal_memcmp);
    LIB_FUNCTION("+P6FRGH4LfA", "libc", 1, "libc", internal_memmove);
    LIB_FUNCTION("NFLs+dRJGNg", "libc", 1, "libc", internal_memcpy_s);
    // NIDs computed with scripts/ps4_names2stubs.py (verified against memcpy/memset/memcmp).
    LIB_FUNCTION("gQX+4GDQjpM", "libc", 1, "libc", bachata_fex_malloc);
    LIB_FUNCTION("2X5agFjKxMc", "libc", 1, "libc", bachata_fex_calloc);
    LIB_FUNCTION("Y7aJ1uydPMo", "libc", 1, "libc", bachata_fex_realloc);
    LIB_FUNCTION("tIhsqj0qsFE", "libc", 1, "libc", bachata_fex_free);
}"""


# ---------------------------------------------------------------------------------------------
# 10. NID-only symbol fallback + host libc for the C runtime libc.prx re-exports
#
# Measured on r20 (2026-11-13): the libc alias registration above took unresolved HLE calls
# from 753 to 703 in a 70 s run -- real but nowhere near enough, and the guest still parked
# at stack.cpp CreateStack. Dumping libc.prx (it is a SELF container, not a bare ELF) showed
# why: it exports 1527 symbols, 1448 of them as (library="libc", module="libc"), and of those
# 1448 only four have an implementation in shadPS4. Meanwhile ~71 of the leftovers already
# exist under a different key -- libSceLibcInternal, libSceFios2, libSceAvPlayer, libSceRtc --
# because the NID is a hash of the symbol name and therefore identical across libraries.
#
# So a record with the same NID and the same type *is* the same function. Linker::Resolve now
# retries by NID alone after the exact match misses, which picks all 71 up in one change.
#
# What that does not cover is the set shadPS4 never implemented at all (printf, the stdio
# family, the time family, qsort). Those are plain host services, so libc_internal_host.cpp
# forwards them to the C library. 71 + 40 = 111 of the 245 remaining keys.
# ---------------------------------------------------------------------------------------------

RESOLVER_H_OLD = b"""    const SymbolRecord* FindSymbol(const SymbolResolver& s) const;
"""

RESOLVER_H_NEW = b"""    const SymbolRecord* FindSymbol(const SymbolResolver& s) const;

    // Resolve by NID alone, ignoring library / module / version.
    //
    // The NID is a hash of the symbol name, so two registrations carrying the same NID and
    // the same type are literally the same function; only the (library, module) bookkeeping
    // differs. Real firmware exposes the same implementation under several of these keys --
    // libc.prx re-exports a large slice of libSceLibcInternal as "libc"#1#"libc" -- and games
    // legitimately import from either. Without this fallback every such import becomes an
    // ENOSYS stub even though a working implementation sits right next to it.
    const SymbolRecord* FindSymbolByNid(std::string_view nid, SymbolType type) const;
"""

RESOLVER_CPP_OLD = b"""    // LOG_INFO(Core_Linker, "Unresolved! {}", name);
    return nullptr;
}
"""

RESOLVER_CPP_NEW = b"""    // LOG_INFO(Core_Linker, "Unresolved! {}", name);
    return nullptr;
}

const SymbolRecord* SymbolsResolver::FindSymbolByNid(std::string_view nid,
                                                     SymbolType type) const {
    // Same walk as FindSymbol, with library / version / module deliberately dropped.
    //
    // The NID is sha1(symbol name + salt), so it identifies the function itself, not the
    // library that happens to announce it. libc.prx re-exports a large slice of what
    // libSceLibcInternal already implements, only under ("libc", "libc") instead of
    // ("libSceLibcInternal", "libSceLibcInternal"); a game importing strlen from libc
    // therefore misses an implementation shadPS4 already has, and gets an ENOSYS stub.
    //
    // Linear on purpose: this only runs after the exact-match lookup has already missed,
    // i.e. once per unresolved import during relocation, not per guest call.
    for (const auto& record : m_symbols) {
        if (record.symbol.type != type || record.symbol.name != nid) {
            continue;
        }
#ifdef SHADPS4_ENABLE_FEX_GUEST_CPU
        // Never hand out a placeholder as if it were an implementation.
        if (record.hle_fallback) {
            continue;
        }
#endif
        return &record;
    }
    return nullptr;
}
"""

LINKER_ALIAS_OLD = b"""        *return_info = *record;
        Core::Devtools::Widget::ModuleList::AddModule(sr.library);
        return true;
    }

    // Check if it an exported function from one of our loaded libraries
"""

LINKER_ALIAS_NEW = b"""        *return_info = *record;
        Core::Devtools::Widget::ModuleList::AddModule(sr.library);
        return true;
    }

    // Second chance: the same NID registered under a different (library, module) pair.
    //
    // libc.prx exports 1448 symbols and re-exports much of libSceLibcInternal, but announces
    // them as "libc"#1#"libc". A game importing strlen from libc therefore misses the exact
    // match libSceLibcInternal already provides and ends up with an ENOSYS stub, even though a
    // working implementation exists. NID is derived from the function name alone (sha1 of
    // name+salt), so equal NID plus equal symbol type means the same function. `record` above
    // is const, hence a separate variable.
    const auto* alias = m_hle_symbols.FindSymbolByNid(sr.name, sym_type);
    if (alias != nullptr) {
        LOG_INFO(Core_Linker, "NID {} resolved via alias {} (wanted {}#{}#{})", sr.name,
                 alias->symbol.name, sr.library, sr.library_version, sr.module);
        *return_info = *alias;
        Core::Devtools::Widget::ModuleList::AddModule(sr.library);
        return true;
    }

    // Check if it an exported function from one of our loaded libraries
"""

# The explicit source list in CMakeLists.txt is not a GLOB, so a new .cpp is invisible until
# it is named here.
CMAKE_LIBC_OLD = b"""                 src/core/libraries/libc_internal/libc_internal_threads.cpp
"""

CMAKE_LIBC_NEW = b"""                 src/core/libraries/libc_internal/libc_internal_host.cpp
                 src/core/libraries/libc_internal/libc_internal_host.h
                 src/core/libraries/libc_internal/libc_internal_threads.cpp
"""

# ---------------------------------------------------------------------------------------------
# RegisterLib() cannot be patched with a fixed byte anchor.
#
# Two trees are in play and they disagree: a pristine CORE_REF ends RegisterLib with
# "Threads(sym);\n}" and no #ifdef block at all, while the r20-era tree already carries the four
# FEX alias calls. Worse, the obvious idempotency marker for "the aliases are already in
# RegisterLib" -- RegisterFexLibcCxaAliases -- also appears in ForceRegisterLib(), so using it
# skipped the edit on every tree and left RegisterLib unpatched. That is the same dead-code
# mistake as r19, one level up.
#
# So find the function body by matching braces and splice into it. Shape-independent, and the
# host registration lands next to the alias calls it depends on either way.
# ---------------------------------------------------------------------------------------------

REGISTERLIB_SIG = b"void RegisterLib(Core::Loader::SymbolsResolver* sym) {"
FORCE_CPP = 'src/core/libraries/libc_internal/libc_internal.cpp'

FEX_BLOCK = b"""#ifdef SHADPS4_ENABLE_FEX_GUEST_CPU
    // FEX paths call libc through the veneer, so the "libc" aliases have to be registered
    // explicitly. They used to be reachable only via ForceRegisterLib(), which nothing called.
    RegisterFexLibcMemoryAliases(sym);
    RegisterFexLibcMathAliases(sym);
    RegisterFexLibcStrAliases(sym);
    RegisterFexLibcCxaAliases(sym);
    // Host-backed printf/stdio/time: libc.prx exports them, shadPS4 never implemented them.
    RegisterFexLibcHostAliases(sym);
#endif
"""

HOST_ONLY = b"""    // Host-backed printf/stdio/time: libc.prx exports them, shadPS4 never implemented them.
    RegisterFexLibcHostAliases(sym);
"""


def arm_libc_internal_cpp(core):
    """Add the host-libc include and register it from inside RegisterLib().

    Deliberately not built out of (old, new) pairs: the two live shapes differ, and the whole
    point of this function is that it does not care which one it is looking at.
    """
    p = read(core / FORCE_CPP)
    data = p.read_bytes()
    nl = b"\r\n" if b"\r\n" in data[:4000] else b"\n"

    if b"RegisterFexLibcHostAliases(sym);" in data:
        print(f"already applied {p} (host libc registered from RegisterLib)")
        return

    # 1. include
    inc_old = b'#include "libc_internal_cxa.h"' + nl
    inc_new = (b'#include "libc_internal_cxa.h"' + nl +
               b'#include "libc_internal_host.h"' + nl)
    if b'#include "libc_internal_host.h"' not in data:
        if data.count(inc_old) != 1:
            sys.exit(f"{p}: include anchor not unique")
        data = data.replace(inc_old, inc_new)

    # 2. locate RegisterLib's body
    i = data.find(REGISTERLIB_SIG)
    if i < 0:
        sys.exit(f"{p}: {REGISTERLIB_SIG.decode()} not found")
    body_start = i + len(REGISTERLIB_SIG)
    depth = 1
    k = body_start
    while k < len(data) and depth > 0:
        ch = data[k:k + 1]
        if ch == b"{":
            depth += 1
        elif ch == b"}":
            depth -= 1
        k += 1
    if depth != 0:
        sys.exit(f"{p}: unbalanced braces after RegisterLib")
    close = k - 1                      # index of RegisterLib's closing brace
    body = data[body_start:close]

    # 3. splice
    if b"RegisterFexLibcCxaAliases(sym);" in body:
        # Shape B: the #ifdef block is already there, add only the host call, before its #endif.
        endif = body.rfind(b"#endif")
        if endif < 0:
            sys.exit(f"{p}: alias calls present but no #endif in RegisterLib")
        block = HOST_ONLY.replace(b"\n", nl)
        new_body = body[:endif] + block + body[endif:]
        note = "host libc appended to the existing FEX block"
    else:
        # Shape A: pristine. Add the whole guarded block.
        block = FEX_BLOCK.replace(b"\n", nl)
        new_body = body + block
        note = "FEX aliases + host libc block inserted"

    data = data[:body_start] + new_body + data[close:]
    p.write_bytes(data)
    print(f"applied {p} ({note})")

    # 4. structural assertion -- the failure this whole function exists to prevent
    reg = data.find(b"void RegisterLib(")
    force = data.find(b"void ForceRegisterLib(")
    host = data.find(b"RegisterFexLibcHostAliases(sym);")
    if not (reg != -1 and force != -1 and host != -1 and reg < host < force):
        sys.exit(f"{p}: host aliases did not land inside RegisterLib")

# Files that live in this repo's overlay and are copied into the core tree verbatim.
OVERLAY_FILES = (
    ("overlay/src/common/bachata_diag.h", "src/common/bachata_diag.h"),
    ("overlay/src/core/libraries/libc_internal/libc_internal_host.cpp",
     "src/core/libraries/libc_internal/libc_internal_host.cpp"),
    ("overlay/src/core/libraries/libc_internal/libc_internal_host.h",
     "src/core/libraries/libc_internal/libc_internal_host.h"),
)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: apply-diagnostics.py <core-dir>")
    core = pathlib.Path(sys.argv[1])

    # 1. files that live in this repo and are dropped into the core tree verbatim
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel_src, rel_dst in OVERLAY_FILES:
        src = read(root / rel_src)
        dst = core / rel_dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        payload = src.read_bytes().replace(b"\r\n", b"\n")
        if dst.is_file() and dst.read_bytes() == payload:
            print(f"already applied {dst} (overlay file)")
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

    # 7b. call tail on the typed adapter, i.e. every implemented HLE call
    edit(core / "src/core/guest_cpu/hle_call_adapter.h",
         [(HCA_TYPED_OLD, HCA_TYPED_NEW)],
         marker=b"BachataDiag::NoteHleCall", note="(last-N HLE call ring)")

    # 7c. keep the tail of the log on disk
    #
    # The marker has to be the injected comment, not the flush call: flush_() at the bottom of the
    # class already ends with "session_file_helper_.flush();", so using that as the marker makes the
    # injection look applied on a pristine tree and it silently never lands.
    edit(core / "src/common/logging/log_file_sink.h",
         [(LOGSINK_OLD, LOGSINK_NEW)],
         marker=b"Bachata: file_helper_ is a fully buffered stdio stream",
         note="(log file flushed per message)")

    # 7d. 16-byte vector arguments: what actually makes the printf family work
    edit(core / "src/core/guest_cpu/hle_call_adapter.h",
         [(HCA_WIDE_OLD, HCA_WIDE_NEW), (HCA_CURSOR_OLD, HCA_CURSOR_NEW),
          (HCA_DECODE_OLD, HCA_DECODE_NEW)],
         marker=b"Bachata: __m128 is __int128 off x86", note="(16-byte vector arguments)")

    # 7e. drain the HLE call ring on the fatal path
    edit(core / "src/core/signals.cpp",
         [(SIG_INCLUDE_OLD, SIG_INCLUDE_NEW), (SIG_TAIL_OLD, SIG_TAIL_NEW)],
         marker=b"Bachata: drain the last-N HLE call ring", note="(HLE call tail at fault)")

    # 7f. sync logging, per-record flush, append-only session file
    edit(core / "src/common/logging/log.cpp",
         [(LOG_SINKS_OLD, LOG_SINKS_NEW), (LOG_FLUSH_OLD, LOG_FLUSH_NEW),
          (LOG_SWITCH_OLD, LOG_SWITCH_NEW)],
         marker=b"Bachata: always synchronous", note="(sync sinks, flush trace, append)")

    # 7g. tiling pipeline cache key includes array_mode
    edit(core / "src/video_core/texture_cache/tile_manager.h",
         [(TM_KEY_OLD, TM_KEY_NEW)],
         marker=b"Bachata: array_mode decides which module gets bound",
         note="(tiling key + array_mode)")

    edit(core / "src/video_core/texture_cache/tile_manager.cpp",
         [(TM_CPP_OLD, TM_CPP_NEW), (TM_LOG_OLD, TM_LOG_NEW)],
         marker=b".array_mode = info.array_mode,", note="(tiling key + array_mode)")

    # 8. startup milestones
    edit(core / "src/core/libraries/pad/pad.cpp",
         [(PAD_INCLUDE_OLD, PAD_INCLUDE_NEW), (PAD_OPEN_OLD, PAD_OPEN_NEW)],
         marker=b"BachataDiag::pad_opens", note="(pad open counter)")

    edit(core / "src/core/libraries/system/systemservice.cpp",
         [(SYS_INCLUDE_OLD, SYS_INCLUDE_NEW), (SYS_STATUS_OLD, SYS_STATUS_NEW),
          (SYS_RECV_OLD, SYS_RECV_NEW), (SYS_EVENT_OLD, SYS_EVENT_NEW),
          (SYS_PARAM_OLD, SYS_PARAM_NEW)],
         marker=b"BachataDiag::sys_getstatus", note="(system service poll counters)")

    edit(core / "src/core/libraries/save_data/savedata.cpp",
         [(SAVE_INCLUDE_OLD, SAVE_INCLUDE_NEW), (SAVE_MOUNT_OLD, SAVE_MOUNT_NEW)],
         marker=b"BachataDiag::save_mounts", note="(save mount counter)")

    edit(core / "src/core/libraries/audio/audioout.cpp",
         [(AUDIO_INCLUDE_OLD, AUDIO_INCLUDE_NEW), (AUDIO_OPEN_OLD, AUDIO_OPEN_NEW)],
         marker=b"BachataDiag::audio_opens", note="(audio open counter)")

    # 7h. Ngs2 allocator callbacks: guest pointers must cross the FEX bridge
    edit(core / "src/core/libraries/ngs2/ngs2_impl.cpp",
         [(NGS2_INCLUDE_OLD, NGS2_INCLUDE_NEW), (NGS2_OPTION_OLD, NGS2_OPTION_NEW),
          (NGS2_HELPERS_OLD, NGS2_HELPERS_NEW), (NGS2_RELEASE_OLD, NGS2_RELEASE_NEW),
          (NGS2_SYSCREATE_OLD, NGS2_SYSCREATE_NEW),
          (NGS2_RACKCREATE_OLD, NGS2_RACKCREATE_NEW)],
         marker=b"Bachata: sceNgs2 hands us guest function pointers",
         note="(Ngs2 allocator callbacks via FEX bridge + host buffer fallback)")

    # 9. libc_internal -- the real fix. MUST run last: it changes what the guest links against.
    # This file is handled by brace-matched splicing, not a byte anchor -- see the note next to
    # FEX_BLOCK. Two shapes exist and the obvious idempotency marker is ambiguous.
    arm_libc_internal_cpp(core)

    # ups-full at CORE_REF already carries the whole group-9 memory change (fex_malloc /
    # fex_calloc / fex_realloc / fex_free + internal_memmove + internal_memcpy_s under
    # "libc"#1#"libc"), so this is a no-op on a current base and only does work on an older
    # tree. Match on internal_memmove, which is the last line the r20 change added.
    edit(core / "src/core/libraries/libc_internal/libc_internal_memory.cpp",
         [(LIBC_MEM_ANCHOR_OLD, LIBC_MEM_ANCHOR_NEW)],
         marker=b"bachata_fex_malloc",
         note="(malloc/calloc/realloc/free for libc)",
         already_marker=b'"libc", internal_memmove')

    # 10. NID-only fallback + host libc. Also has to run after group 9: it assumes the alias
    # registration is already in RegisterLib(), and it re-registers it plus the host set.
    edit(core / "src/core/loader/symbols_resolver.h",
         [(RESOLVER_H_OLD, RESOLVER_H_NEW)],
         marker=b"FindSymbolByNid", note="(NID-only lookup declared)")

    edit(core / "src/core/loader/symbols_resolver.cpp",
         [(RESOLVER_CPP_OLD, RESOLVER_CPP_NEW)],
         marker=b"SymbolsResolver::FindSymbolByNid", note="(NID-only lookup implemented)")

    edit(core / "src/core/linker.cpp",
         [(LINKER_ALIAS_OLD, LINKER_ALIAS_NEW)],
         marker=b"resolved via alias", note="(linker retries by NID)")

    edit(core / "CMakeLists.txt",
         [(CMAKE_LIBC_OLD, CMAKE_LIBC_NEW)],
         marker=b"libc_internal_host.cpp", note="(host libc added to the source list)")

    # NOTE: libc_internal.cpp is NOT edited here. arm_libc_internal_cpp() in group 9 owns it,
    # because it needs brace matching rather than a byte anchor; a second edit here would be
    # both redundant and shape-dependent.

    # Cheap sanity pass: the strings the CI verifier greps for in the binary.
    print("--- diagnostics check ---")
    misses = []
    for path, needle in (
        (core / "src/common/bachata_diag.h", b"bachata-heartbeat.txt"),
        (core / "src/common/bachata_diag.h", b"ev=p%llu"),
        (core / "src/main.cpp", b"BachataDiag::StartHeartbeat"),
        (core / "src/main.cpp", b"BachataDiag::UnbufferLogs"),
        (core / "src/core/libraries/libs.cpp",
         b'{"libSceNgs2.sprx", Libraries::Ngs2::RegisterLib}'),
        (core / "src/core/libraries/sysmodule/sysmodule_internal.cpp",
         b"is pre-registered at startup"),
        (core / "src/core/libraries/videoout/driver.cpp", b"BachataDiag::presents"),
        (core / "src/core/libraries/gnmdriver/gnmdriver.cpp", b"BachataDiag::gnm_submits"),
        (core / "src/core/guest_cpu/hle_call_adapter.h", b"BachataDiag::NoteUnresolvedCall"),
        (core / "src/core/guest_cpu/hle_call_adapter.h", b"BachataDiag::NoteHleCall"),
        (core / "src/core/guest_cpu/hle_call_adapter.h", b"IsWideVectorArgument"),
        (core / "src/core/guest_cpu/hle_call_adapter.h", b"NextVectorWide"),
        (core / "src/core/signals.cpp", b"HLE call tail at fault"),
        (core / "src/common/logging/log_file_sink.h",
         b"Bachata: file_helper_ is a fully buffered stdio stream"),
        (core / "src/common/logging/log.cpp", b"Bachata: always synchronous"),
        (core / "src/common/logging/log.cpp", b"Bachata: session start (append, sync)"),
        (core / "src/video_core/texture_cache/tile_manager.cpp", b".array_mode = info.array_mode,"),
        (core / "src/video_core/texture_cache/tile_manager.cpp", b"array_mode={} samples={}"),
        (core / "src/video_core/texture_cache/tile_manager.h",
         b"Bachata: array_mode decides which module gets bound"),
        (core / "src/common/bachata_diag.h", b"FormatCallTail"),
        (core / "src/core/libraries/pad/pad.cpp", b"BachataDiag::pad_opens"),
        (core / "src/core/libraries/system/systemservice.cpp", b"BachataDiag::sys_recvevent"),
        (core / "src/core/libraries/save_data/savedata.cpp", b"BachataDiag::save_mounts"),
        (core / "src/core/libraries/audio/audioout.cpp", b"BachataDiag::audio_opens"),
        (core / "src/core/libraries/ngs2/ngs2_impl.cpp", b"CallBufferHandler"),
        # The guest-pointer path and the host fallback, asserted on code that stays on one source
        # line. Do NOT assert on a user facing message here: the fallback warning is written as
        # "using host "\n "memory" across two C++ string literals, so the prose never appears
        # contiguously in the file and the assertion fails on a perfectly good tree (r26 stopped
        # the build on exactly that).
        (core / "src/core/libraries/ngs2/ngs2_impl.cpp", b"RunGuestFunctionOrAbort"),
        (core / "src/core/libraries/ngs2/ngs2_impl.cpp", b"BufferPool().Acquire(required)"),
        (core / "src/core/libraries/ngs2/ngs2_impl.cpp", b"SystemCreateWithAllocator -> {}"),
        # 9. libc aliases. Accepted in either shape: the canonical rewritten comment (r19/r20
        # trees) or the upstream block that ups-full already carries. Both register the same
        # four alias families, so only that is asserted, not the comment text.
        (core / "src/core/libraries/libc_internal/libc_internal.cpp",
         b"RegisterFexLibcMemoryAliases(sym);"),
        (core / "src/core/libraries/libc_internal/libc_internal.cpp",
         b"RegisterFexLibcCxaAliases(sym);"),
        (core / "src/core/libraries/libc_internal/libc_internal_memory.cpp",
         b'LIB_FUNCTION("gQX+4GDQjpM", "libc", 1, "libc",'),
        (core / "src/core/libraries/libc_internal/libc_internal_memory.cpp",
         b'LIB_FUNCTION("+P6FRGH4LfA", "libc", 1, "libc",'),
        # 10. NID-only fallback + host libc
        (core / "src/core/loader/symbols_resolver.h", b"FindSymbolByNid"),
        (core / "src/core/loader/symbols_resolver.cpp", b"SymbolsResolver::FindSymbolByNid"),
        (core / "src/core/linker.cpp", b"resolved via alias"),
        (core / "CMakeLists.txt", b"libc_internal_host.cpp"),
        (core / "src/core/libraries/libc_internal/libc_internal.cpp",
         b"RegisterFexLibcHostAliases"),
        (core / "src/core/libraries/libc_internal/libc_internal_host.cpp", b"host_qsort"),
        (core / "src/core/libraries/libc_internal/libc_internal_host.cpp", b"GMpvxPFW924"),
    ):
        ok = needle in path.read_bytes()
        print(f"  {'OK  ' if ok else 'MISS'} {path.relative_to(core)} :: {needle.decode()}")
        if not ok:
            # Report every failure in one pass: aborting on the first one turned a single bad
            # needle into a full 20 minute rebuild before anyone could see the rest.
            misses.append(f"{path.relative_to(core)} :: {needle.decode()}")

    # Structural check the grep above cannot make: RegisterFexLibcHostAliases has to be in
    # RegisterLib(), which is the one the linker actually calls. Landing it in ForceRegisterLib()
    # would still satisfy every string check while registering nothing at runtime.
    libc_cpp = (core / "src/core/libraries/libc_internal/libc_internal.cpp").read_bytes()
    reg = libc_cpp.find(b"void RegisterLib(")
    force = libc_cpp.find(b"void ForceRegisterLib(")
    host = libc_cpp.find(b"RegisterFexLibcHostAliases(sym);")
    placed = reg != -1 and force != -1 and host != -1 and reg < host < force
    print(f"  {'OK  ' if placed else 'MISS'} libc_internal.cpp :: host aliases inside "
          f"RegisterLib (not ForceRegisterLib)")
    if not placed:
        misses.append("libc_internal.cpp :: host aliases not inside RegisterLib")

    # Second structural check: no LIB_FUNCTION() may take a C-variadic function.
    #
    # The FEX bridge instantiates TypedHleCallAdapter<Return (*)(Args...)> per registration, and
    # an ellipsis never matches that partial specialisation -- the primary template stays
    # undefined and clang reports "implicit instantiation of undefined template". It is a compile
    # error, not a runtime limitation, so it would fail the whole build (this is exactly how the
    # printf/fprintf/sprintf/sscanf registrations broke the first r21 build). Only the va_list
    # twins may be registered.
    host_src = (core / "src/core/libraries/libc_internal/libc_internal_host.cpp").read_text(
        encoding="utf-8", errors="replace")
    registered = re.findall(
        r'LIB_FUNCTION\("[^"]+",\s*"[^"]+",\s*\d+,\s*"[^"]+",\s*(\w+)\)', host_src)
    param_lists = {}
    for m in re.finditer(r'\b(\w+)\s*\(([^;{}]*?)\)\s*\{', host_src):
        param_lists.setdefault(m.group(1), []).append(m.group(2))
    variadic = [fn for fn in registered
                if any("..." in p for p in param_lists.get(fn, []))]
    print(f"  {'OK  ' if not variadic else 'MISS'} libc_internal_host.cpp :: "
          f"{len(registered)} registrations, none C-variadic"
          + (f" (offenders: {', '.join(variadic)})" if variadic else ""))
    if variadic:
        misses.append("libc_internal_host.cpp :: C-variadic registration")

    if misses:
        print(f"--- {len(misses)} assertion(s) failed ---")
        for m in misses:
            print(f"      {m}")
        sys.exit(1)


if __name__ == "__main__":
    main()
