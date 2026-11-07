# 移植说明与技术报告

## 1. 事实基础（全部有出处）

| 项 | 内容 | 出处 |
|---|---|---|
| 症状 | 序章结算后自动存档卡死、无法存档 | shadps4-game-compatibility #2263（CUSA07651）、#2449 |
| 根因 | `sceSaveDataUmountWithBackup` 后等 type-1 backup 完成回调，回调注册是 stub | shadPS4 PR #5226 描述 |
| 上游修复 | PR #5226「save_data: deliver backup completion callbacks」，2026-10-28 合并，随 v0.21.0（10-30）发布 | PR 页面 / v0.21.0 release notes |
| 安卓核心源码 | `zenithblue-oss/shadps4-arm64`，基线为 2026-08-27 的上游快照 | 该仓库 commit 列表（末条上游提交 2026-08-27） |
| 安卓前端 | **不在公开源码中**（无 `android/`、无安卓 CMake preset、CI 只构建 Win/mac/Linux） | 仓库根目录树 + `.github/workflows/build.yml` |
| 核心可交叉编译 | `runtime/scripts/build-shadps4-arm64.sh` 交叉产出 `aarch64` Linux 二进制 `bin/shadps4-arm64`，开关 `-DENABLE_BACHATA_RUNTIME=ON -DENABLE_FEX_GUEST_CPU=ON` | 该脚本 |

## 2. 这次改了什么

等价于上游 PR #5226，落在 2026-08-27 基线上，4 个文件 +101/−23：

- `save_backup.cpp`
  - 备份线程从 `std::jthread` 换成 `Kernel::Thread`（模拟线程，保证 guest 回调有合法 TLS/栈）
  - 备份失败不再静默 `return`，改为抛 `filesystem_error` 并落 `error_code`
  - 备份完成后调用 `DispatchBackupEvent(req)`
  - `StopThread` 改为真正 `Join` 并清空队列；新增 `g_backup_stopping` 原子标志
- `save_backup.h`：`BackupRequest` 增加 `error_code`
- `savedata.cpp`
  - 新增回调表 `g_event_callback` / `g_event_userdata` / `g_in_backup_callback`
  - 实现 `DispatchBackupEvent`（加锁、填事件、`SCOPE_EXIT` 复位 in-callback 标记）
  - `sceSaveDataRegisterEventCallback` / `sceSaveDataUnregisterEventCallback` 从 stub 改为真实现
  - `sceSaveDataGetEventResult` 返回真实 `errorCode`
  - `sceSaveDataTerminate` 在回调上下文内返回 `BUSY`，避免自己 Join 自己
- `savedata.h`：新增 `OrbisSaveDataEventCallback` 类型，修函数签名

`.patch` 已用 `git apply --check` 在目标树副本上验证可干净应用。

## 3. 为什么"改包"这条路走不通

Bachata S4+ 是从 **0.1.8 官方二进制**起手的改包项目（README 自述），手法是 smali/资源层。
但存档逻辑在**编译后的 C++ 核心二进制**里：

- 它不是一个返回值开关，而是「注册回调 → 备份线程完成后派发事件 → 游戏侧解除等待」的
  跨线程状态机，smali 层无法表达
- 二进制里 `sceSaveDataRegisterEventCallback` 原本就是一条 `return 0` 的 stub，
  没有任何可"打补丁"的分支逻辑可改

所以无论怎么去签名校验/安全机制，**都必须重新编译核心二进制**。
"去掉安全机制"只解决最后一公里（自编译产物被允许加载），解决不了"要编译"这件事。

## 4. 可行路线（按成本排序）

1. **等官方同步**（最省事）：官方 v0.2.5 早于修复 16 天，下一次同步 ≥ v0.21.0 内核即带修复。
2. **云端自编译 + 换内核二进制**（本仓库路线）：
   - Actions 里用 `build-shadps4-arm64.sh` 交叉编译出 `shadps4-arm64`
   - 下载官方 APK，定位运行时容器，替换其中的二进制，`zipalign` + `apksigner` 重新签名
   - 若应用对运行时做完整性校验（`components.lock.json` / 摘要清单），需同步更新哈希
   - 剩余未知项：APK 内二进制存放位置 → 由 `inspect-apk` 作业输出
3. **自己实现前端**（成本最高）：公开源码缺 `android/`，需要重写 JNI + UI + 运行时解包。

## 5. 云端构建环境（已按上游实现核对）

上游没有把 arm64 runtime 放进它自己的 GitHub Actions（`.github/workflows/build.yml` 只构建
Windows / macOS / Linux 桌面版），所以构建环境只能从脚本本身反推。结论有三条硬约束：

| 约束 | 依据 |
|---|---|
| 必须在 **Debian trixie** 容器里构建 | `runtime/scripts/install-debian-runtime-deps.sh` 的 `BACHATA_DEBIAN_IMAGE` 默认值、`ID == debian` 断言、`dpkg --print-architecture == amd64` 断言 |
| 依赖必须用上游脚本装 | 它校验 18 个包的 amd64/arm64 版本严格一致（`check_matching_candidate`），手写 apt 列表会在版本漂移时出错 |
| 不能直接跑在 Ubuntu runner 上 | 构建脚本硬编码 `CMAKE_FIND_ROOT_PATH=/usr/aarch64-linux-gnu`；Ubuntu 多架构库在 `/usr/lib/aarch64-linux-gnu`，路径对不上 |

另外，FEX（guest CPU 解释器）是构建期联网拉取的：`build-fexcore-smoke-aarch64.sh` 读
`runtime/locks/components.lock.json` 里 `fex` 的 URL + 40 位 revision，用
`checkout-component.sh` 以 `--filter=blob:none` 浅克隆，然后打 `runtime/patches/fex-fexcore-only.patch`。
锁定的 FEX 版本是 `f2b679f6028ce1c38875233aecfcf5d3f8ebecec`，公网可匿名拉，CI 不需要任何凭据。

## 6. 尚未验证

- 编译：本机无 Linux 工具链（WSL 被安全策略拦截）、磁盘不足（C: 342MB / F: 1.9GB），
  本地无法编译；需云端验证
- 运行：需真机（红魔电竞平板 Pro，8 Gen 3 / Adreno 750 / 12GB）
- 建议验证点：进游戏后首次自动存档不卡；序章结算可过；退出重进能读到存档
- 参考设置（上游测试者反馈）：需要 Readback Linear Images + 至少 Readbacks Mode Relaxed
