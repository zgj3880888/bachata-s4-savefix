#!/usr/bin/env bash
# Apply the ARM64 port of upstream shadPS4 0.21.1 onto a Bachata core checkout.
#
# This is not a feature patch — it rebases the entire Bachata ARM64 tree (FEX guest CPU,
# Android SAF broker, managed patch sessions, Bachata runtime CLI) onto upstream
# shadps4-emu/shadPS4 0.21.1. It must land exactly: a fuzzy apply would leave the two
# halves interleaved and produce a core that links but cannot start under the 0.2.7 app.
#
# Submodules are handled separately because `git apply` cannot retarget a gitlink whose
# checkout does not exist yet, and because the port renames one (externals/dear_imgui ->
# externals/imgui) and adds several (protobuf, zstd, zarchive, abseil-cpp, imgui).
#
# Usage: scripts/apply-port.sh <core-checkout-dir>
set -euo pipefail

core_dir=${1:?usage: apply-port.sh <core-checkout-dir>}
repo_root=$(builtin cd -- "${BASH_SOURCE[0]%/*}/.." && builtin pwd -P)
patch="$repo_root/patches/0001-upstream-021-arm64-port.patch"
submodule_list="$repo_root/config/submodule-shas.txt"

[ -f "$patch" ] || {
  echo "missing $patch" >&2
  exit 1
}
[ -f "$submodule_list" ] || {
  echo "missing $submodule_list" >&2
  exit 1
}

cd "$core_dir"
git config core.autocrlf false || true

# Build the exclude list from the port's own submodule table: every gitlink is retargeted
# below, so the diff hunks for those paths must not be touched here. Real file changes that
# happen to live under externals/ (externals/CMakeLists.txt, stb headers, ...) still apply.
exclude_args=()
while read -r path _sha; do
  [ -n "$path" ] || continue
  exclude_args+=(--exclude="$path")
done <"$submodule_list"

if git apply --check --binary "${exclude_args[@]}" --exclude=.gitmodules "$patch" 2>/dev/null; then
  git apply --binary "${exclude_args[@]}" --exclude=.gitmodules "$patch"
else
  echo "port patch does not apply onto this core checkout." >&2
  echo "Expected base: Nyces/shadps4-arm64 main (2026-10-02 snapshot)." >&2
  echo "Regenerate with: git -C ups-full diff --binary arm64main HEAD" >&2
  exit 1
fi
echo "applied $(basename "$patch")"

echo "== aligning submodules with upstream 0.21.1"
cp "$repo_root/config/gitmodules" .gitmodules
while read -r path sha; do
  [ -n "$path" ] || continue
  git update-index --add --cacheinfo "160000,$sha,$path"
done <"$submodule_list"

# 0.21.1 renamed externals/dear_imgui to externals/imgui. The old gitlink survives the apply
# because its diff hunk is excluded above, and `git submodule sync` aborts with
# "No url found for submodule path" on any index gitlink that .gitmodules no longer declares --
# even though later steps would have replaced it. Drop such entries before syncing.
git ls-files -s | awk '$1 == "160000" { print $4 }' | while read -r stale; do
  case "$stale" in externals/*) ;; *) continue ;; esac
  grep -qF "path = $stale" .gitmodules && continue
  echo "dropping stale gitlink $stale"
  git update-index --force-remove "$stale"
  rm -rf -- "$stale"
done

git submodule sync --recursive

# Pass 1: top level. No --depth here: the port pins exact revisions, and shallow fetches by SHA
# only work when the commit happens to be a branch tip. Individual entries carry
# `shallow = true` in .gitmodules, so the big ones (tracy, ffmpeg-core) still come down
# without full history.
git submodule update --init --jobs 8

# Pass 2+: nested submodules, walking one level per pass (git only recurses into submodules
# that are already initialised, so a single --recursive pass would miss depth 3).
#
# externals/mesa-kosmickrisp is skipped on purpose. CMake only adds it under
# `if (APPLE AND NOT ENABLE_SYSTEM_VULKAN AND NOT ENABLE_TESTS)`, so no Linux/Android target
# ever looks at it -- and its nested externals/mesa pins a commit its remote refuses to serve
# (`upload-pack: not our ref`), which aborts the whole recursive update.
for _pass in 1 2; do
  git submodule foreach --quiet --recursive '
    case "$displaypath" in
      externals/mesa-kosmickrisp*) exit 0 ;;
    esac
    git submodule update --init --jobs 4
  '
done

echo "== verifying port markers"
fail=0
check() {
  if grep -qF -- "$2" "$1"; then
    echo "OK   $2  ($1)"
  else
    echo "MISS $2  ($1)"
    fail=1
  fi
}

# Platform glue that the Android frontend depends on to launch and drive the core.
check src/main.cpp '--bachata-socket'
check src/main.cpp '--bachata-storage-root'
check src/main.cpp '--bachata-saf-socket'
check src/main.cpp '--bachata-saf-token'
check src/main.cpp '--patch-session'
check src/main.cpp 'StartInputReader'
check src/emulator.cpp 'MountSaf'
check src/core/file_sys/fs.cpp 'MountSaf'
check src/core/libraries/kernel/threads/pthread.cpp 'DeliverGuestOrbisSignal'
check src/core/fex/fex_guest_engine.cpp 'DeliverGuestOrbisSignal'
check src/core/guest_cpu/hle_call_adapter.h 'MakeHleCallAdapter'
check src/common/memory_patcher.cpp 'ApplyManagedSessionFile'
check runtime/scripts/build-shadps4-arm64.sh 'externals/imgui/misc/fonts'

# Upstream 0.21.1 identity: proves the tree really is the new base, not the 2026-06 snapshot.
check src/core/libraries/system/userservice.cpp 'OrbisUserServiceEventCallback'
check src/core/libraries/kernel/threads/semaphore.cpp 'Common::SlotId'

[ "$fail" -eq 0 ] || {
  echo "port markers missing; refusing to build" >&2
  exit 1
}
echo "port verified"
git diff --stat | tail -3
