# 网页上传版（不需要命令行、不需要 token）

GitHub 连接器掉线时用这条路，全程在浏览器点几下就行。

## 这次要传什么

目标目录：`F:\workbuddy2\2026-11-07-18-32-20\port-bachata-savefix\repo`

**7 个条目，全传**：

```
.github/      工作流（要传，拖不进去就看方式 B）
config/       .gitmodules + 48 个子模块 revision
patches/      0001-upstream-021-arm64-port.patch（7.1 MB）
scripts/      apply-port.sh / repack-runtime.py / selftest-repack.py
docs/         移植记录
.gitignore
README.md
```

`patches/` 里那个 7.1 MB 的差分是整套移植，别漏。

## 方式 A：拖文件夹（60 秒）

1. 打开 https://github.com/new
   - Repository name 随便填，比如 `shadps4-arm64-021`
   - 选 **Public**（Actions 免费额度无限）
   - **不要**勾 Add a README / .gitignore / license（要空仓库）
   - 点 Create repository

2. 新仓库页面点 **uploading an existing file**（或 Add file → Upload files）

3. 把上面 7 个条目一起拖进浏览器上传区

4. 底部 Commit changes

5. 点 **Actions** → 左边选 `build-arm64-core` → 等它自动跑
   （推送就会触发；没自动跑就点 Run workflow）

## 方式 B：拖拽失败时（`.github` 或 `config` 文件夹拖不进去）

1. 方式 A 的第 1、3、4 步照做，但**跳过 `.github`**，先传其余 6 个
2. 仓库里点 **Actions** → 若出现提示就点 **set up a workflow yourself**
3. 把 `.github/workflows/build.yml` 的内容整份粘贴进网页编辑器
4. 文件名保持 `build.yml`，Commit changes

`config/` 如果也拖不进去：它只有两个文本文件。在仓库页 `Add file → Create new file`，
文件名分别填 `config/gitmodules` 和 `config/submodule-shas.txt`，内容对照本地文件粘贴。
`config/submodule-shas.txt` 少一行都会导致子模块版本不对，编出来跑不起来 —— 建议用
`Add file → Upload files` 单独把 `config` 文件夹再试一次。

## 跑起来之后

`build-arm64-core` 大约 60–120 分钟（48 个子模块 + FEX 交叉编译 + 核心）。日志里这几个
`==` 段落是健康信号：

```
applied 0001-upstream-021-arm64-port.patch
== aligning submodules with upstream 0.21.1
== verifying port markers
   OK   --bachata-socket  (src/main.cpp)
   ...
port verified
```

产物在 **Actions → 该次运行 → Artifacts → `shadps4-arm64-021`**：

| 文件 | 用途 |
|---|---|
| `shadps4-arm64` | 换进 APK 的内核二进制（ELF64 / AArch64） |
| `needed.txt` | 它依赖的 .so 清单，用来核对 APK 里 guest rootfs 是否齐 |
| `shadps4-arm64.sha256` | 校验值 |
| `port-applied.stat` | 差分实际落地统计（审计用） |
| `build.log` | 完整编译日志（失败时看这个） |

拿到后换进 APK：

```bash
python scripts/repack-runtime.py BachataS4Plus.apk shadps4-arm64 patched.apk \
    --needed needed.txt --label bachata-021
```

## 失败时怎么回报

把 `build.log` 最后 200 行贴出来就行。已知最容易挂的几处：

1. **`port patch does not apply onto this core checkout`**
   → Bachata 的 `main` 漂移了。需要重新取基线并重生成差分
   （`git diff --binary arm64main HEAD`），不是改脚本能解决的。

2. **`port markers missing; refusing to build`**
   → 差分应用了一部分。看日志里哪条 `MISS`，那条对应的文件没落地。

3. **子模块 checkout 失败（`reference is not a tree` / `rev-parse` 不对）**
   → `config/submodule-shas.txt` 与 GitHub 上该仓库的可见性不符，或上游 force-push 过。

4. **`install-debian-runtime-deps.sh` 的多架构版本一致性检查**
   → Debian 索引短暂不同步，重跑一次通常就好。

5. **FEXCore 编译失败**
   → FEX 版本钉在 `runtime/locks/components.lock.json`（`f2b679f6`），与上游 0.21.1 的
     HLE 层不匹配时会报符号/结构体错误。这类问题必须改
     `src/core/guest_cpu/` 或 `src/core/fex/` 里的胶水，不能靠换 FEX 版本绕过。
