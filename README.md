# Bachata S4 存档修复移植（save_data backup callbacks）

把上游 shadPS4 的存档回调修复（shadps4-emu/shadPS4 PR **#5226**，随 **v0.21.0** 发布）
移植到安卓端 Bachata S4 的 ARM64 核心源码
[`zenithblue-oss/shadps4-arm64`](https://github.com/zenithblue-oss/shadps4-arm64)，
并把编出来的内核换进官方 APK。

## 为什么需要它

PS4《GUNDAM BREAKER 3》（CUSA07651 / CUSA03594 家族）在 `sceSaveDataUmountWithBackup`
之后会等待一个 **type-1 backup 完成回调**。原实现把
`sceSaveDataRegisterEventCallback` 写成 stub，回调永远不来，于是：

- 新游戏第一次自动存档卡在 `Saving...`
- 序章/新手关结算后自动存档卡死，无法结算、无法存档
- 伴随日志噪音 `file_system.cpp ... sceKernelStat: error = 2`（不是根因）

本补丁按上游实现补齐：回调注册/注销、备份完成后在**模拟线程**（有 guest TLS/栈）
派发事件、错误码随事件回传、终止流程保护。

## 官方 APK 里内核实测长什么样

这一点决定了整个改包策略，所以先摆证据（v0.2.5，211,009,680 字节，428 个条目）：

| 条目 | 压缩 | 大小 | 说明 |
|---|---|---|---|
| `assets/runtime/runtime.zip` | **STORED** | 111,227,803 | 整个 Debian guest 用户态，397 个条目 |
| `assets/runtime/manifest.json` | STORED | 66,330 | 每个条目的 `path/size/sha256` + `runtimeVersion` |
| `assets/runtime/native-runtime.json` | STORED | 503 | JNI `.so` 的哈希，**与内核无关** |
| `lib/arm64-v8a/libbachata_native_runtime.so` | DEFLATE | 85,583,216 | 安卓侧 JNI 宿主，**不含内核** |
| `assets/catalog/ps4_catalog.db` | DEFLATE | 117,723,136 | 兼容库 |

`runtime.zip` 内部与内核相关的五个文件：

```
host/shadps4-arm64-fex                        75,323,616   ← 被替换的主体
usr/share/bachata/guest-runtime.json             162       {"variant","revision","sha256"}
usr/share/bachata/guest-runtime.txt              127       variant=… / revision=… / sha256=…
usr/share/bachata/shadps4-arm64-fex-needed.txt    97       ld-linux + libc + …
(package.json 里叫 runtime.zip，manifest 里叫 manifest.json，两者必须一致)
```

所以换内核不是「替换一个 ELF」，而是**五处必须同时改对**：二进制本体、两个
`guest-runtime` 描述符、依赖清单、以及 `manifest.json`（含派生的 `runtimeVersion`）。
`runtimeVersion` 的算法已用官方 manifest 反解验证：把每个条目的
`path\0sha256\n` 顺序喂进 sha256，取前 12 位十六进制，得到 `runtime-b7db79837e56`，
与官方声明完全一致。

## 内容

| 文件 | 作用 |
|---|---|
| `patches/0001-save-data-backup-completion-callbacks.patch` | 4 个文件，+101/−23，等价于上游 PR #5226 |
| `scripts/apply-port.sh` | 严格应用补丁（漂移直接失败，不做模糊匹配）+ 落地标记校验 |
| `scripts/repack-runtime.py` | 精准改包：换核心 + 同步 4 个描述符 + 重算 manifest/`runtimeVersion` + 自检 |
| `scripts/selftest-repack.py` | 离线自测：构造同结构的合成 APK，验证条目顺序/压缩方式/哈希链全部成立 |
| `.github/workflows/build.yml` | 两个作业：交叉编译核心 → 换进官方 APK、对齐、重签 |

## 状态

- [x] 补丁按目标树重写，`git apply --check` 通过
- [x] 云端交叉编译通过（`debian:trixie`，产物 ELF64/AArch64，解释器 `/lib/ld-linux-aarch64.so.1`）
- [x] 官方 APK 结构反解完毕，`runtimeVersion` 推导算法与官方一致
- [x] 改包脚本离线自测通过
- [ ] 真机验证存档（需要装机实测）

## 云端构建

工作流 `build-savefix-core`：

**作业 `build`**（`debian:trixie` 容器）

1. 拉取核心源码 + 40 余个子模块，严格应用补丁
2. 跑项目自带的 `runtime/scripts/install-debian-runtime-deps.sh`（arm64 多架构依赖）
3. 跑项目自带的 `runtime/scripts/build-shadps4-arm64.sh` 交叉编译
4. 校验 ELF，`aarch64-linux-gnu-strip --strip-debug` 裁剪，上传内核 + `needed.txt` + diff + 日志

**作业 `repack-apk`**

1. 下载官方 APK 并校验 sha256
2. `scripts/repack-runtime.py`：换 `host/shadps4-arm64-fex`，同步描述符与 manifest，
   并检查「新二进制需要的每个 `.so` 都已在 runtime 里」——缺一个就直接失败
3. `zipalign -p -f 4` → 生成临时 keystore → `apksigner sign` → `apksigner verify`
4. 上传 `patched.apk` + 校验日志

**为什么必须在 Debian 容器里跑**：上游构建脚本硬编码了
`CMAKE_FIND_ROOT_PATH=/usr/aarch64-linux-gnu`，这是 Debian 交叉工具链的路径；
Ubuntu runner 的多架构库在 `/usr/lib/aarch64-linux-gnu`，直接跑配置阶段就找不到 X11。

## 安装前必须知道

- 自编译产物的签名与官方不同，**装之前必须先卸载官方版**；存档在应用私有目录，卸载会一并清掉。
  如果序章存档还要，先用官方版的 Save Manager 导出。
- 本仓库**不包含**任何 PS4 游戏、固件、密钥或 APK 本体，构建时从官方 release 现下。

## 许可

补丁源自 shadPS4（GPL-2.0-or-later）；改包脚本针对 Bachata S4 公开导出的运行时结构编写。
整体以 GPL-2.0-or-later 提供。
