# shadPS4 → 安卓 ARM64 移植（合上游 0.21.1）

把 **PC 版 shadPS4**（[`shadps4-emu/shadPS4`](https://github.com/shadps4-emu/shadPS4)，0.21.1）
的模拟器内核移植成 **Android / AArch64** 形态，挂到 **Bachata S4 / S4+** 的安卓壳子里运行。

上游只有 Windows / Linux / macOS 的 x64 构建，没有任何能直接在手机上跑的目标。这里补的是
那三块缺口：

| 缺口 | 做法 |
|---|---|
| **指令集**：PS4 游戏是 x86-64 代码，ARM 手机执行不了 | 接 **FEX-Emu** 做 JIT guest CPU；HLE 符号经 x86 veneer 注册，guest 回调经 FEX 进入 |
| **存储**：安卓 scoped storage 不允许按路径重开文件 | **SAF broker**（RPC + `SCM_RIGHTS` 传 fd）+ `SafFsBackend` 挂载 `/app0`、`/hostapp` |
| **宿主**：手机上没有桌面窗口系统，也没有 SDL 输入轮询 | **Bachata runtime IPC**：socket 上报 running/error/stopped，收手柄与触摸采样，内嵌 X server |

产出一个 ELF64/AArch64 的 `shadps4-arm64`，由安卓前端在 Debian guest 用户态里拉起。

## 怎么出包（不需要命令行、不需要 token）

1. 在 GitHub 建一个**空**公开仓库（不要勾 README / .gitignore / license）
2. 把本目录**全部内容**（含 `.github`、`config`、`patches`、`scripts`、`docs`）拖进
   `Add file → Upload files`，Commit
3. **Actions** → `build-arm64-core` → Run workflow
4. 约 60–120 分钟（拉 48 个子模块 + 编 FEX + 编内核）

产物在 **Actions → 该次运行 → Artifacts → `shadps4-arm64-021`**：

| 文件 | 用途 |
|---|---|
| `shadps4-arm64` | 换进 APK 的内核（ELF64 / AArch64，已 strip debug） |
| `needed.txt` | 它依赖的 `.so` 清单，核对 APK 里 guest rootfs 是否齐 |
| `shadps4-arm64.sha256` | 校验值 |
| `port-applied.stat` / `build.log` | 审计与排错 |

## 怎么换进 APK

官方 APK 里**没有**独立的 `bin/shadps4-arm64`。内核在
`assets/runtime/runtime.zip` 内名为 `host/shadps4-arm64-fex`，并且有多处哈希要同步：

```
host/shadps4-arm64-fex                            ← 被替换的主体
usr/share/bachata/guest-runtime.json              {"variant","revision","sha256"}
usr/share/bachata/guest-runtime.txt               variant=… / revision=… / sha256=…
usr/share/bachata/shadps4-arm64-fex-needed.txt    ld-linux + libc + …
assets/runtime/manifest.json                      每个条目的 path/size/sha256
```

`runtimeVersion` 是把每个条目的 `path\0sha256\n` 顺序喂进 sha256 取前 12 位十六进制，
所以改完内核必须重算，否则前端会拒绝加载。

`scripts/repack-runtime.py` 一次做完（换条目 + 重算哈希 + 重签名）：

```bash
python scripts/repack-runtime.py BachataS4Plus.apk shadps4-arm64 patched.apk \
    --needed needed.txt --label bachata-021
```

先看 APK 内部结构：`python scripts/repack-runtime.py --inspect BachataS4Plus.apk`

## 这个仓库里的东西

```
config/gitmodules          合并后的 .gitmodules（上游 0.21.1 + Bachata 专有子模块并集）
config/submodule-shas.txt  48 个子模块的精确 revision
patches/0001-upstream-021-arm64-port.patch   整树移植差分（720 文件，7.1 MB）
scripts/apply-port.sh      在 Bachata 基树上应用该差分 + 对齐子模块 + 校验移植标记
scripts/repack-runtime.py  把新内核换进官方 APK
```

差分是 `git diff --binary arm64main HEAD` 生成的，**必须严格应用**（`git apply --check`
通过才继续）：它把整棵树从 2026-06-20 的上游基线搬到 0.21.1，模糊应用会留下半合并的
FEX/SAF 胶水，编出来的内核能链接但起不来。

子模块不放进差分里，而是由 `config/submodule-shas.txt` 逐条对齐 —— 上游把
`externals/dear_imgui` 改名成 `externals/imgui`，还新增了 protobuf / zstd / zarchive /
abseil-cpp，`git apply` 没法给一个尚不存在的 checkout 重定向 gitlink。

## 移植里最容易踩的三处

1. **合并基线**。Bachata 树那个「Publish hardened ARM64 client source」是整树覆盖提交，
   git 记录的父提交不是真实基线。真实基线是 2026-06-20 的 `5ddd87d7`，用
   `git replace --graft` 改父提交后，3 路合并冲突从 181 降到 89。
2. **guest 信号必须经 FEX**。guest 装的 `SIGSEGV` / `SIGUSR1`（Unity GC 用它做
   stop-the-world）handler 是 x86 代码，直接在 ARM 上 `call` 会跳进未映射内存。
   统一走 `Core::Fex::DeliverGuestOrbisSignal` 排到 HLE 边界执行。
3. **SAF 下不能按路径重开文件**。`std::filesystem::copy_file`、`IOFile(path)` 这类调用在
   scoped storage 里会 `EACCES`。要走 `Common::FS::SetFdOpenHook` 拿已打开的 fd。

## 已知边界

- 上游 0.21.1 的 `video_core` / `shader_recompiler` 全部取官方实现。Bachata 树里的
  Adreno 专用调优（tile manager scratch 池、`vk_presenter` 诊断、`vk_instance` 扩展
  可选化）**没有回植** —— 先保证能编能跑，再按实测瓶颈逐条加回。
- `VK_EXT_robustness2` 在上游是硬要求，Adreno/Mali 驱动不一定暴露；当前降级为警告。
- 桌面专属路径（`SetIcon` 的 X11 图标属性、SDL 手柄扫描）在 `ENABLE_BACHATA_RUNTIME`
  下短路，安卓侧改由 IPC 提供输入。

## 历史文档

`docs/PORT-NOTES.md` 是上一轮「只移植 save_data 备份回调」工作的推理记录。该补丁的
结论已被上游 0.21.1 内置（`OrbisSaveDataEventCallback` / `DispatchBackupEvent`），
本仓库不再包含它，只保留文档作参考。
