#!/usr/bin/env bash
# build-trollstore-carplay.sh — 越狱 / TrollStore 版 LemonMusic IPA，完整保留 CarPlay
#
# 原理：TrollStore 用 CoreTrust 绕过，安装时会保留 IPA 内二进制上由 ldid 预签的
# 「任意 entitlements」。因此不需要 Apple 的 CarPlay 授权，com.apple.developer.carplay-audio
# 直接写进 ldid 签名即可；同时保留 Info.plist 的 CarPlay scene 声明。
#
# 用法（macOS + Xcode 16+，或 GitHub Actions macos-15 runner）：
#   bash scripts/build-trollstore-carplay.sh                         # 默认 iOS 17.1 起
#   bash scripts/build-trollstore-carplay.sh --min-ios 18.0          # 设备是 iOS 18+
#   bash scripts/build-trollstore-carplay.sh --bundle com.wangfei.lemonmusic
#   bash scripts/build-trollstore-carplay.sh --no-appgroup           # 不注入 App Group
#   bash scripts/build-trollstore-carplay.sh --add-app-id            # 额外注入 application-identifier
#
# 前置：brew install ldid；Xcode 16+（iOS 18 SDK）；能访问 github.com 拉 SPM 二进制依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MIN_IOS="17.1"
BUNDLE="com.example.lemonmusic"
USE_APPGROUP=1
ADD_APP_ID=0
OUT="$ROOT/build/trollstore"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --min-ios)     MIN_IOS="$2"; shift 2 ;;
    --bundle)      BUNDLE="$2"; shift 2 ;;
    --no-appgroup) USE_APPGROUP=0; shift ;;
    --add-app-id)  ADD_APP_ID=1; shift ;;
    --out)         OUT="$2"; shift 2 ;;
    -h|--help)     sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "错误：iOS 应用只能在 macOS 上用 Xcode 构建（当前 $(uname -s)）。" >&2
  exit 1
fi
for tool in xcodebuild python3 ldid; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "缺少 $tool" >&2
    [[ "$tool" == ldid ]] && echo "  brew install ldid" >&2
    exit 1
  }
done

PBX="LemonMusic.xcodeproj/project.pbxproj"
SRC_ENT="LemonMusic/LemonMusic-iOS.entitlements"
SRC_WENT="LemonMusicWidgets/LemonMusicWidgets.entitlements"
SRC_INFO="LemonMusic/Info.plist"
WIDGET_SHARED="WidgetShared/PlaybackWidgetSnapshot.swift"

for f in "$PBX" "$SRC_ENT" "$SRC_WENT" "$SRC_INFO" "$WIDGET_SHARED"; do
  [[ -f "$f" ]] || { echo "缺少文件: $f" >&2; exit 1; }
done
mkdir -p "$OUT"

echo "==> 1/6 改写工程（不改 CarPlay entitlement，只改签名身份与最低系统）"
# 备份放在 build/ 下，不污染 git 工作树
mkdir -p "$OUT/backup"
for f in "$PBX" "$SRC_ENT" "$SRC_WENT" "$SRC_INFO" "$WIDGET_SHARED"; do
  cp "$f" "$OUT/backup/$(echo "$f" | tr '/' '_').bak"
done

MIN_IOS="$MIN_IOS" BUNDLE="$BUNDLE" PBX="$PBX" WIDGET_SHARED="$WIDGET_SHARED" \
python3 - <<'PY'
import os, re
min_ios, bundle = os.environ["MIN_IOS"], os.environ["BUNDLE"]

p = os.environ["PBX"]
s = open(p, encoding="utf-8").read()

# 作者的 Team ID 必须去掉：本流程完全不签名，交给 ldid / TrollStore。
s, n_team = re.subn(r"^\s*DEVELOPMENT_TEAM = [^;]+;\n", "", s, flags=re.M)
s, n_target = re.subn(r"IPHONEOS_DEPLOYMENT_TARGET = [0-9.]+;",
                      "IPHONEOS_DEPLOYMENT_TARGET = %s;" % min_ios, s)
for old, new in [
    ("top.btop.music.widgets", bundle + ".widgets"),
    ("top.btop.musicUITests", bundle + ".uitests"),
    ("top.btop.musicTests", bundle + ".tests"),
    ("top.btop.music", bundle),
]:
    s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("   移除 DEVELOPMENT_TEAM %d 处；部署目标 -> iOS %s（%d 处）；Bundle -> %s" %
      (n_team, min_ios, n_target, bundle))

w = os.environ["WIDGET_SHARED"]
t = open(w, encoding="utf-8").read().replace("group.top.btop.music", "group." + bundle)
open(w, "w", encoding="utf-8").write(t)
print("   WidgetShared App Group -> group.%s" % bundle)
PY

# iOS 17 兼容守卫：源码已是 iOS 17 版时只做静态校验；若被上游还原则自动修补。
# 无条件执行 —— 保证推到 CI 的代码里绝无 iOS 18+/26+ 符号，不白烧 macOS 分钟。
echo "==> 1.5/6 iOS 17 兼容守卫（校验 / 兜底修补）"
python3 scripts/ios17_compat.py

echo "==> 2/6 生成 ldid 用的 entitlements（保留 CarPlay）"
APP_ENT="$OUT/LemonMusic.trollstore.entitlements.plist"
WIDGET_ENT="$OUT/LemonMusicWidgets.trollstore.entitlements.plist"
MIN_IOS="$MIN_IOS" BUNDLE="$BUNDLE" USE_APPGROUP="$USE_APPGROUP" ADD_APP_ID="$ADD_APP_ID" \
SRC_ENT="$SRC_ENT" SRC_WENT="$SRC_WENT" APP_ENT="$APP_ENT" WIDGET_ENT="$WIDGET_ENT" \
python3 - <<'PY'
import os, plistlib

with open(os.environ["SRC_ENT"], "rb") as fh:
    main = plistlib.load(fh)
with open(os.environ["SRC_WENT"], "rb") as fh:
    widget = plistlib.load(fh)

bundle = os.environ["BUNDLE"]
use_appgroup = os.environ["USE_APPGROUP"] == "1"

# 关键：CarPlay 音频 entitlement 原样保留（越狱/TrollStore 不需要 Apple 授权）。
main["com.apple.developer.carplay-audio"] = True

if use_appgroup:
    group = "group." + bundle
    main["com.apple.security.application-groups"] = [group]
    widget["com.apple.security.application-groups"] = [group]
else:
    main.pop("com.apple.security.application-groups", None)
    widget.pop("com.apple.security.application-groups", None)

if os.environ["ADD_APP_ID"] == "1":
    # 少数系统服务会顺带检查这两个键；默认不注入，按需打开。
    main["application-identifier"] = "TROLLSTORE." + bundle
    main["com.apple.developer.team-identifier"] = "TROLLSTORE"

with open(os.environ["APP_ENT"], "wb") as fh:
    plistlib.dump(main, fh)
with open(os.environ["WIDGET_ENT"], "wb") as fh:
    plistlib.dump(widget, fh)
print("   主 App:", sorted(main))
print("   Widget:", sorted(widget))
PY

echo "==> 3/6 解析 SPM 依赖 + 不签名归档"
SPM="$OUT/SourcePackages"
xcodebuild -resolvePackageDependencies \
  -project LemonMusic.xcodeproj -scheme LemonMusic \
  -clonedSourcePackagesDirPath "$SPM"

ARCHIVE="$OUT/LemonMusic.xcarchive"
xcodebuild \
  -project LemonMusic.xcodeproj \
  -scheme LemonMusic \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -derivedDataPath "$OUT/DerivedData" \
  -archivePath "$ARCHIVE" \
  -clonedSourcePackagesDirPath "$SPM" \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= \
  clean archive

APP="$ARCHIVE/Products/Applications/LemonMusic.app"
[[ -d "$APP" ]] || { echo "归档里没有 $APP" >&2; exit 1; }

echo "==> 4/6 修 Info.plist（Xcode 15+ 自动生成会吞掉自定义键）"
# 全局已验证的坑：GENERATE_INFOPLIST_FILE=YES 会把自定义 plist 换成生成版，
# UIApplicationSceneManifest / ATS / UIBackgroundModes 可能丢失。这里以生成版为底，
# 把源码 plist 的关键键强制合并回去，并补 UISupportsCarPlay。
MIN_IOS="$MIN_IOS" BUNDLE="$BUNDLE" SRC_INFO="$SRC_INFO" APP="$APP" \
python3 - <<'PY'
import os, plistlib

app = os.environ["APP"]
dst = os.path.join(app, "Info.plist")
with open(dst, "rb") as fh:
    info = plistlib.load(fh)
with open(os.environ["SRC_INFO"], "rb") as fh:
    src = plistlib.load(fh)

for key in ("UIApplicationSceneManifest", "NSAppTransportSecurity", "UIBackgroundModes",
            "CFBundleURLTypes", "UILaunchScreen"):
    if key in src:
        info[key] = src[key]

info["UISupportsCarPlay"] = True
info["MinimumOSVersion"] = os.environ["MIN_IOS"]
info["CFBundleIdentifier"] = os.environ["BUNDLE"]

scene = (info.get("UIApplicationSceneManifest", {})
             .get("UISceneConfigurations", {})
             .get("CPTemplateApplicationSceneSessionRoleApplication"))
if not scene:
    print("错误：CarPlay scene 声明丢失", file=__import__("sys").stderr)
    raise SystemExit(1)

with open(dst, "wb") as fh:
    plistlib.dump(info, fh)
print("   CarPlay scene: %s / %s" %
      (scene[0].get("UISceneClassName"), scene[0].get("UISceneDelegateClassName")))
print("   MinimumOSVersion=%s, UISupportsCarPlay=%s, ATS=%s, BackgroundModes=%s" %
      (info["MinimumOSVersion"], info["UISupportsCarPlay"],
       "NSAppTransportSecurity" in info, info.get("UIBackgroundModes")))
PY

echo "==> 5/6 ldid 预签（TrollStore 安装时保留 entitlements）"
rm -rf "$APP/_CodeSignature"
ldid -S"$APP_ENT" "$APP"
APPEX="$APP/PlugIns/LemonMusicWidgets.appex"
if [[ -d "$APPEX" ]]; then
  rm -rf "$APPEX/_CodeSignature"
  ldid -S"$WIDGET_ENT" "$APPEX"
else
  echo "  警告：未找到 Widget 扩展 .appex（小组件不可用，主 App 不受影响）" >&2
fi

echo "==> 校验签名里的 entitlements"
ldid -e "$APP/LemonMusic" > "$OUT/signed-entitlements.plist"
python3 - "$OUT/signed-entitlements.plist" <<'PY'
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
assert d.get("com.apple.developer.carplay-audio") is True, "CarPlay entitlement 未写进签名"
print("   ldid -e 主程序 entitlements:", sorted(d))
PY

echo "==> 6/6 打包 IPA 并校验"
IPA_DIR="$OUT/ipa"
rm -rf "$IPA_DIR/Payload/LemonMusic.app"
mkdir -p "$IPA_DIR/Payload"
cp -R "$APP" "$IPA_DIR/Payload/"
IPA="$OUT/LemonMusic.trollstore.carplay.ipa"
(cd "$IPA_DIR" && zip -qry "$IPA" Payload)

MIN_IOS="$MIN_IOS" BUNDLE="$BUNDLE" IPA="$IPA" python3 - <<'PY'
import os, plistlib, zipfile, sys
ipa = os.environ["IPA"]
with zipfile.ZipFile(ipa) as z:
    names = z.namelist()
    info_name = next(n for n in names if n.endswith(".app/Info.plist"))
    info = plistlib.loads(z.read(info_name))
    exe = next((n for n in names if n.endswith(".app/LemonMusic")), None)
    widget = next((n for n in names if n.endswith(".appex/Info.plist")), None)
missing = []
if info.get("MinimumOSVersion") != os.environ["MIN_IOS"]:
    missing.append("MinimumOSVersion")
if (info.get("UIApplicationSceneManifest", {}).get("UISceneConfigurations", {})
        .get("CPTemplateApplicationSceneSessionRoleApplication") is None):
    missing.append("CarPlay scene")
if info.get("UISupportsCarPlay") is not True:
    missing.append("UISupportsCarPlay")
if "NSAppTransportSecurity" not in info:
    missing.append("NSAppTransportSecurity")
if info.get("UIBackgroundModes") != ["audio"]:
    missing.append("UIBackgroundModes=audio")
if not exe:
    missing.append("主可执行文件")
if missing:
    print("IPA 关键内容缺失:", missing, file=sys.stderr)
    raise SystemExit(1)
print("   IPA 校验 OK | bundle=%s | min=%s | CarPlay scene=OK | widget=%s" %
      (info.get("CFBundleIdentifier"), info.get("MinimumOSVersion"), bool(widget)))
PY

echo "==> 完成"
echo "    IPA : $IPA"
shasum -a 256 "$IPA"
echo "    把 IPA 传到设备后用 TrollStore 打开安装；CarPlay 需接车机实测（模拟器无效）。"
echo "    还原工程：把 $OUT/backup/*.bak 改回对应原路径即可。"
