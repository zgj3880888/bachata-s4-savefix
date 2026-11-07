#!/usr/bin/env bash
# 把重新编译出来的 shadps4-arm64 换进官方 Bachata S4 APK，并重签名。
#
# 用法：
#   scripts/repack.sh app.apk -            out.apk     # 只出布局报告（不做改动）
#   scripts/repack.sh app.apk new-binary   out.apk     # 替换 + 重签名
#
# 依赖：unzip / tar / python3 / zstd（或 xz）/ zipalign / apksigner
# 在 Debian/Ubuntu 上：apt-get install -y unzip zip python3 zstd xz-utils zipalign apksigner
#
# 设计前提（为什么不能只做字符串替换）：
#   * 二进制可能直接被放进 ZIP（assets/xxx 或 lib/<abi>/*.so），也可能先被塞进一个
#     tar.zst / tar.xz 运行时容器里再进 ZIP —— 两种情况处理方式完全不同。
#   * 运行时容器内可能带 components.lock.json 之类的哈希清单，只换二进制不改哈希，
#     App 启动时会拒绝加载，所以换完要回写哈希。
#   * APK 有 v2/v3 签名，改完任何字节签名即失效，必须 zipalign + apksigner 重签。
set -euo pipefail

apk=${1:?usage: repack.sh <apk> <new-binary|-> <out.apk>}
newbin=${2:?usage: repack.sh <apk> <new-binary|-> <out.apk>}
out=${3:?usage: repack.sh <apk> <new-binary|-> <out.apk>}

report_only=0
case "$newbin" in
  - | /dev/null) report_only=1 ;;
  *) [ -f "$newbin" ] || { echo "新二进制不存在: $newbin" >&2; exit 1; } ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
declare -a notes=()
note() { notes+=("$*"); printf '[note] %s\n' "$*"; }
die() { printf '\n[FATAL] %s\n' "$*" >&2; exit 1; }

########################################################################
# 1. 布局报告
########################################################################
echo "=============================================================="
echo " APK: $apk  ($(stat -c%s "$apk" 2>/dev/null || echo '?') bytes)"
echo " SHA256: $(sha256sum "$apk" | cut -d' ' -f1)"
echo "=============================================================="
unzip -l "$apk" >"$work/entries.txt"
echo "条目总数: $(unzip -Z1 "$apk" | wc -l)"
echo
echo "--- 疑似运行时载荷 / 原生二进制 ---"
grep -Ei 'shadps4|runtime|rootfs|debian|\.tar|\.zst|\.xz|\.so$|lib/arm64|assets/' "$work/entries.txt" \
  | head -200 || echo "(无匹配)"
echo
echo "--- 顶层目录分布 ---"
unzip -Z1 "$apk" | awk -F/ 'NF>1{print $1}' | sort | uniq -c | sort -rn | head -20
echo

if [ "$report_only" -eq 1 ]; then
  echo "RESULT: 仅报告模式，未做任何修改。"
  exit 0
fi

########################################################################
# 2. 找目标：直接放 ZIP 里，还是在某个载荷容器里
########################################################################
mapfile -t direct < <(unzip -Z1 "$apk" | grep -Ei '(^|/)(shadps4-arm64|libshadps4[^/]*\.so)$' || true)
mapfile -t payloads < <(unzip -Z1 "$apk" | grep -Ei '\.(tar\.zst|tar\.xz|tar\.gz|tar|tgz)$' || true)

echo "直接命中的二进制条目: ${#direct[@]}"
printf '  %s\n' "${direct[@]:-<none>}"
echo "载荷容器: ${#payloads[@]}"
printf '  %s\n' "${payloads[@]:-<none>}"
echo

[ "${#direct[@]}" -gt 0 ] || [ "${#payloads[@]}" -gt 0 ] \
  || die "既没找到直接的二进制条目，也没找到载荷容器。请把上面的布局报告发出来。"

newsha=$(sha256sum "$newbin" | cut -d' ' -f1)
newsize=$(stat -c%s "$newbin")
echo "新二进制: size=$newsize sha256=$newsha"
echo

mkdir -p "$work/unpacked"
unzip -q -o "$apk" -d "$work/unpacked"

########################################################################
# 3. 在载荷里找 bin/shadps4-arm64
########################################################################
targets=()

if [ "${#payloads[@]}" -gt 0 ]; then
  for p in "${payloads[@]}"; do
    src="$work/unpacked/$p"
    [ -f "$src" ] || { note "载荷条目不存在: $p"; continue; }
    case "$p" in
      *.tar.zst) zstd -q -d -f "$src" -o "$work/payload.tar" ;;
      *.tar.xz) xz -q -d -c "$src" >"$work/payload.tar" ;;
      *.tar.gz | *.tgz) gzip -q -d -c "$src" >"$work/payload.tar" ;;
      *.tar) cp "$src" "$work/payload.tar" ;;
      *) continue ;;
    esac
    d="$work/payload-$(echo "$p" | tr '/.' '__')"
    mkdir -p "$d"
    tar -xf "$work/payload.tar" -C "$d"
    while IFS= read -r f; do
      targets+=("${p}|${f#"$d"/}|$f")
    done < <(find "$d" -type f -name 'shadps4-arm64' 2>/dev/null)
  done
fi

for e in "${direct[@]}"; do
  targets+=("*|$e|$work/unpacked/$e")
done

echo "可替换目标: ${#targets[@]}"
printf '  %s\n' "${targets[@]:-<none>}"
echo
[ "${#targets[@]}" -gt 0 ] || die "载荷解开了但里面没有 bin/shadps4-arm64。请把报告发出来确认路径。"

########################################################################
# 4. 替换
########################################################################
declare -A touched_payload=()

for t in "${targets[@]}"; do
  IFS='|' read -r payload rel abs <<<"$t"
  oldsha=$(sha256sum "$abs" | cut -d' ' -f1)
  echo "替换 $rel  (旧 $oldsha → 新 $newsha)"
  install -m 0755 "$newbin" "$abs"

  # 同载荷里的哈希清单要跟着改，否则 App 启动时校验失败
  if [ "$payload" != "*" ]; then
    touched_payload["$payload"]=1
    payloaddir="$work/payload-$(echo "$payload" | tr '/.' '__')"
    while IFS= read -r lf; do
      [ -n "$lf" ] || continue
      if grep -q "$oldsha" "$lf" 2>/dev/null; then
        sed -i "s/$oldsha/$newsha/g" "$lf"
        note "已更新哈希清单 $(basename "$lf"): $oldsha → $newsha"
      fi
    done < <(grep -rlE '(\.sha256$|\.json$|\.txt$)' "$payloaddir" 2>/dev/null || true)
  fi
done

########################################################################
# 5. 重打载荷
########################################################################
for p in "${!touched_payload[@]}"; do
  d="$work/payload-$(echo "$p" | tr '/.' '__')"
  src="$work/unpacked/$p"
  case "$p" in
    *.tar.zst) tar -cf "$work/new.tar" -C "$d" . && zstd -q -19 -f "$work/new.tar" -o "$src" ;;
    *.tar.xz) tar -cf "$work/new.tar" -C "$d" . && xz -q -9 -c "$work/new.tar" >"$src" ;;
    *.tar.gz | *.tgz) tar -cf "$work/new.tar" -C "$d" . && gzip -q -9 -c "$work/new.tar" >"$src" ;;
    *.tar) tar -cf "$src" -C "$d" . ;;
  esac
  note "已重打载荷 $p（$(stat -c%s "$src") bytes）"
done

########################################################################
# 6. 重新打包 APK：保持每个条目的压缩方式，剔除旧签名
########################################################################
python3 - "$apk" "$work/unpacked" "$work/unsigned.apk" <<'PY'
import os, sys, zipfile

src_apk, root, dst = sys.argv[1], sys.argv[2], sys.argv[3]
SIG_EXT = ('.SF', '.RSA', '.DSA', '.EC')


def skip(name):
    return name.startswith('META-INF/') and name.upper().endswith(SIG_EXT)


with zipfile.ZipFile(src_apk) as zi, zipfile.ZipFile(dst, 'w') as zo:
    n = 0
    for info in zi.infolist():
        if info.is_dir() or skip(info.filename):
            continue
        path = os.path.join(root, info.filename)
        if not os.path.exists(path):
            continue
        out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
        out.compress_type = info.compress_type
        out.external_attr = info.external_attr
        out.create_system = info.create_system
        zo.writestr(out, open(path, 'rb').read())
        n += 1
print('repacked entries:', n)
PY

########################################################################
# 7. 对齐 + 重签
########################################################################
have() { command -v "$1" >/dev/null 2>&1; }

aligned="$work/aligned.apk"
if have zipalign; then
  zipalign -p -f 4 "$work/unsigned.apk" "$aligned"
  note "zipalign 完成"
else
  cp "$work/unsigned.apk" "$aligned"
  note "缺少 zipalign，跳过对齐（APK 仍可能安装，但不推荐长期使用）"
fi

ks=${SAVEFIX_KEYSTORE:-$work/debug.keystore}
if [ -z "${SAVEFIX_KEYSTORE:-}" ] && ! [ -f "$ks" ] && have keytool; then
  keytool -genkeypair -keystore "$ks" -storepass android -keypass android \
    -alias savefix -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=savefix" >/dev/null 2>&1
  note "已生成临时调试密钥库（与官方签名不同，需要先卸载官方版才能安装）"
fi

if have apksigner && [ -f "$ks" ]; then
  apksigner sign --ks "$ks" --ks-pass pass:android --key-pass pass:android \
    --out "$out" "$aligned"
  apksigner verify --print-certs "$out" | head -5
  note "已用 $ks 重签"
else
  cp "$aligned" "$out"
  note "未签名产物已输出：$out（需手工 apksigner sign）"
fi

########################################################################
# 8. 汇总
########################################################################
echo
echo "=============================================================="
echo " 输出: $out ($(stat -c%s "$out") bytes)"
echo " sha256: $(sha256sum "$out" | cut -d' ' -f1)"
echo "=============================================================="
printf '%s\n' "${notes[@]:-}"
echo
echo "RESULT: OK"
echo "提示：换内核后首次启动看 logcat 里 Lib_SaveData 的输出——"
echo "      修复生效时会出现 'Delivering backup event: type=1'。"
