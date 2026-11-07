#!/usr/bin/env bash
# Apply the save_data backup-callback port onto a checkout of the Bachata ARM64 core.
#
# The patch in patches/ was authored against the tree at CORE_REF. It must apply
# strictly: a fuzzy apply here would silently mangle the cross-thread state machine
# that fixes the "Saving..." deadlock, so we fail loudly instead of guessing.
#
# Usage: scripts/apply-port.sh <core-checkout-dir>
set -euo pipefail

core_dir=${1:?usage: apply-port.sh <core-checkout-dir>}
repo_root=$(builtin cd -- "${BASH_SOURCE[0]%/*}/.." && builtin pwd -P)

cd "$core_dir"
git config core.autocrlf false || true

applied=0
for p in "$repo_root"/patches/*.patch; do
  [ -e "$p" ] || continue

  if git apply --check "$p" 2>/dev/null; then
    git apply "$p"
    echo "applied local patch (strict): $(basename "$p")"
    applied=1
    break
  fi

  if git apply --check --ignore-whitespace "$p" 2>/dev/null; then
    git apply --ignore-whitespace "$p"
    echo "applied local patch (whitespace tolerant): $(basename "$p")"
    applied=1
    break
  fi

  echo "patch did not apply: $(basename "$p")" >&2
done

if [ "$applied" -ne 1 ]; then
  cat >&2 <<'EOF'
ERROR: no patch applied.

The core tree has drifted from the revision this port was written against.
Regenerate patches/0001-*.patch against the new CORE_REF before building:

    git -C core format-patch -1 --stdout > patches/0001-save-data-backup-completion-callbacks.patch
EOF
  exit 1
fi

echo "== verifying port markers"
grep -qF 'OrbisSaveDataEventCallback' src/core/libraries/save_data/savedata.h
grep -qF 'void DispatchBackupEvent' src/core/libraries/save_data/savedata.cpp
grep -qF 'Kernel::Thread g_backup_thread' src/core/libraries/save_data/save_backup.cpp
grep -qF 'g_event_callback(&event, g_event_userdata)' src/core/libraries/save_data/savedata.cpp
grep -qF 'if (g_in_backup_callback)' src/core/libraries/save_data/savedata.cpp
echo "port verified"
git diff --stat
