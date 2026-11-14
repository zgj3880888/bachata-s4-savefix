// SPDX-FileCopyrightText: Copyright 2024 shadPS4 Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later

#pragma once

#include "common/types.h"

namespace Core::Loader {
class SymbolsResolver;
}

namespace Libraries::LibcInternal {
#ifdef SHADPS4_ENABLE_FEX_GUEST_CPU
// Host-backed C runtime entry points that libc.prx exports under the
// "libc"#1#"libc" key. See libc_internal_host.cpp for why this is separate.
void RegisterFexLibcHostAliases(Core::Loader::SymbolsResolver* sym);
#endif
} // namespace Libraries::LibcInternal
