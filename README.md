# Bachata S4 保存修复移植（save_data backup callbacks）

把上游 shadPS4 的存档回调修复（shadps4-emu/shadPS4 PR **#5226**，随 **v0.21.0** 发布）
移植到安卓端 Bachata S4 的 ARM64 核心源码
[`zenithblue-oss/shadps4-arm64`](https://github.com/zenithblue-oss/shadps4-arm64)。

## 为什么需要它

PS4《GUNDAM BREAKER 3》（CUSA07651 / CUSA03594 家族）在 `sceSaveDataUmountWithBackup`
之后会等待一个 **type-1 backup 完成回调**。原实现把
`sceSaveDataRegisterEventCallback` 写成 stub，回调永远不来，于是：

- 新游戏第一次自动存档卡在 `Saving...`
- 序章/新手关结算后自动存档卡死，无法结算、无法存档
- 伴随日志噪音 `file_system.cpp ... sceKernelStat: error = 2`（不是根因）

本补丁按上游实现补齐：回调注册/注销、备份完成后在**模拟线程**（有 guest TLS/栈）
派发事件、错误码随事件回传、终止流程保护。

## 内容

| 文件 | 改动 |
|---|---|
| `patches/0001-save-data-backup-completion-callbacks.patch` | 4 个文件，+101/−23，等价于上游 PR #5226 |
| `scripts/apply-port.sh` | 严格应用补丁（漂移就报错，不做模糊匹配）+ 落地标记校验 |
| `scripts/repack.sh` | 真正的换二进制脚本：载荷探测（直放 / tar.zst / tar.xz）→ 替换 → 回写哈希清单 → 重打 → zipalign → apksigner |
| `.github/workflows/build.yml` | 三个作业：交叉编译核心 → 换进官方 APK 并重签 → 导出 APK 布局报告 |
| `scripts/push-local.sh` | 本机一条命令建仓并推送（已 `git init` 并完成首次提交） |

## 状态

- [x] 补丁已按目标树（2026-08-27 基线）逐处重写，`git apply --check` 通过
- [ ] 编译验证（走 GitHub Actions 云端构建）
- [ ] 打进 APK 并在真机验证存档

## 云端构建

工作流 `build-savefix-core`（`.github/workflows/build.yml`）：

1. 在 `debian:trixie` 容器里拉取核心源码 + 子模块，应用补丁
2. 跑核心仓库自带的 `runtime/scripts/install-debian-runtime-deps.sh`（arm64 多架构依赖）
3. 跑核心仓库自带的 `runtime/scripts/build-shadps4-arm64.sh` 交叉编译
4. 校验产物是 `ELF64 / AArch64` 且解释器为 `/lib/ld-linux-aarch64.so.1`，上传 `shadps4-arm64` + diff + 构建日志

**为什么必须在 Debian 容器里跑**：上游构建脚本硬编码了
`CMAKE_FIND_ROOT_PATH=/usr/aarch64-linux-gnu`，这是 Debian 交叉工具链生成的路径；
Ubuntu runner 的 arm64 多架构库在 `/usr/lib/aarch64-linux-gnu`，直接跑会对不上。

另外两个作业：

- `repack-apk`（依赖 `build`）：下载官方 APK，用 `scripts/repack.sh` 把新内核换进去、
  回写哈希清单、`zipalign` + `apksigner` 重签，产出可直接安装的 `patched.apk`
- `inspect-apk`：只导出官方 APK 的内部布局报告（`apk-layout`），不改任何东西


## 推送到 GitHub

目录已经 `git init` 并完成首次提交，推上去就会自动触发构建：

```bash
# 方式 A：装了 GitHub CLI（推荐，一条命令建仓+推送）
bash scripts/push-local.sh

# 方式 B：手工
git remote add origin https://github.com/<你的账号>/bachata-s4-savefix.git
git push -u origin main
```

推上去后在 Actions 里看 `build-savefix-core`，产物在 Artifacts → `shadps4-arm64-savefix`。

## 换掉 APK 里的二进制（说明）

Bachata S4 的 APK 由私有 Gradle 前端打包，公开源码里**没有** `android/` 前端，
所以只能走「官方 APK + 替换运行时二进制 + 重新签名」这条路：

```bash
unzip app.apk -d apk/                 # 找到 <runtime 容器>/bin/shadps4-arm64
#   → 解包容器、替换 bin/shadps4-arm64、按原压缩格式回包
zipalign -p -f 4 apk-unsigned.apk apk-aligned.apk
apksigner sign --ks debug.keystore --out app-signed.apk apk-aligned.apk
```

若应用内部对运行时做了完整性校验（`components.lock.json` / 摘要清单），
还需要同步更新对应哈希，否则运行时会拒绝加载。

## 许可

补丁内容源自 shadPS4（GPL-2.0-or-later）与 Bachata S4 的公开导出源码，
同样以 GPL-2.0-or-later 提供。本仓库**不包含**任何 PS4 游戏、固件、密钥或 APK 本体。
