#!/usr/bin/env bash
# prepare-sideload.sh — 把 LemonMusic 改成自己的签名配置，并在 macOS 上产出可侧载的 IPA
#
# 用法：
#   ./scripts/prepare-sideload.sh --team ABCDE12345 --bundle com.myname.lemonmusic
#       -> Xcode 自动签名归档，产出已签名 IPA（AltStore / Sideloadly / Xcode 直接安装）
#   ./scripts/prepare-sideload.sh --unsigned
#       -> 不签名归档，产出未签名 IPA（TrollStore / 越狱 / 后续自行重签）
#   ./scripts/prepare-sideload.sh --team XYZ --bundle com.myname.lemonmusic --strip-appgroup
#       -> 免费 Apple ID 场景：同时移除 App Group entitlement（小组件实时数据会降级为静态）
#
# 前置条件：macOS + Xcode 16 或更高（需 iOS 18 SDK）、能访问 github.com 拉取 SPM 二进制依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TEAM=""
BUNDLE="com.example.lemonmusic"
STRIP_CARPLAY=1
STRIP_APPGROUP=0
OUT="$ROOT/build-output"

usage() {
  cat <<'EOF'
用法: scripts/prepare-sideload.sh [选项]
  --team TEAMID         你的 Apple Developer Team ID（10 位）。给出后走自动签名。
  --bundle BUNDLE_ID    主 App 的 Bundle ID 前缀，默认 com.example.lemonmusic
  --keep-carplay        保留 CarPlay entitlement（仅当你的账号已获 Apple CarPlay 授权时使用）
  --strip-appgroup      移除 App Group entitlement（免费 Apple ID 无法签 App Group）
  --out DIR             构建产物目录，默认 ./build-output
  --unsigned            强制不签名（与不传 --team 等价）
  -h, --help            显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --team)            TEAM="$2"; shift 2 ;;
    --bundle)          BUNDLE="$2"; shift 2 ;;
    --keep-carplay)    STRIP_CARPLAY=0; shift ;;
    --strip-appgroup)  STRIP_APPGROUP=1; shift ;;
    --out)             OUT="$2"; shift 2 ;;
    --unsigned)        TEAM=""; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "错误：iOS 应用只能在 macOS 上用 Xcode 构建（当前 $(uname -s)）。" >&2
  exit 1
fi
if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "错误：找不到 xcodebuild，请先安装 Xcode 并执行 sudo xcode-select -s /Applications/Xcode.app/Contents/Developer" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：找不到 python3" >&2
  exit 1
fi

PBX="LemonMusic.xcodeproj/project.pbxproj"
APP_ENT="LemonMusic/LemonMusic-iOS.entitlements"
WIDGET_ENT="LemonMusicWidgets/LemonMusicWidgets.entitlements"
INFO="LemonMusic/Info.plist"
WIDGET_SHARED="WidgetShared/PlaybackWidgetSnapshot.swift"

for f in "$PBX" "$APP_ENT" "$WIDGET_ENT" "$INFO" "$WIDGET_SHARED"; do
  [[ -f "$f" ]] || { echo "缺少文件: $f" >&2; exit 1; }
  cp "$f" "$f.bak"
done

echo "==> 改写工程配置（原文件已备份为 *.bak）"
TEAM="$TEAM" BUNDLE="$BUNDLE" STRIP_CARPLAY="$STRIP_CARPLAY" STRIP_APPGROUP="$STRIP_APPGROUP" \
PBX="$PBX" APP_ENT="$APP_ENT" WIDGET_ENT="$WIDGET_ENT" INFO="$INFO" WIDGET_SHARED="$WIDGET_SHARED" \
python3 - <<'PY'
import os, re, plistlib

team = os.environ["TEAM"]
bundle = os.environ["BUNDLE"]
strip_carplay = os.environ["STRIP_CARPLAY"] == "1"
strip_appgroup = os.environ["STRIP_APPGROUP"] == "1"

# --- pbxproj：Team ID / Bundle ID ---
p = os.environ["PBX"]
s = open(p, encoding="utf-8").read()
if team:
    s, n = re.subn(r"DEVELOPMENT_TEAM = [^;]+;", "DEVELOPMENT_TEAM = %s;" % team, s)
    print("  DEVELOPMENT_TEAM -> %s (%d 处)" % (team, n))
else:
    s, n = re.subn(r"^\s*DEVELOPMENT_TEAM = [^;]+;\n", "", s, flags=re.M)
    print("  移除作者硬编码的 DEVELOPMENT_TEAM (%d 处)，构建不签名" % n)

replacements = [
    ("top.btop.music.widgets", bundle + ".widgets"),
    ("top.btop.musicUITests", bundle + ".uitests"),
    ("top.btop.musicTests", bundle + ".tests"),
    ("top.btop.music", bundle),
]
for old, new in replacements:
    s = s.replace(old, new)
open(p, "w", encoding="utf-8").write(s)
print("  PRODUCT_BUNDLE_IDENTIFIER -> %s[.widgets/.tests/.uitests]" % bundle)

# --- entitlements：CarPlay / App Group ---
for key, path in (("app", os.environ["APP_ENT"]), ("widget", os.environ["WIDGET_ENT"])):
    with open(path, "rb") as fh:
        d = plistlib.load(fh)
    changed = []
    if strip_carplay and "com.apple.developer.carplay-audio" in d:
        d.pop("com.apple.developer.carplay-audio")
        changed.append("CarPlay")
    if strip_appgroup:
        if "com.apple.security.application-groups" in d:
            d.pop("com.apple.security.application-groups")
            changed.append("AppGroup")
    elif "com.apple.security.application-groups" in d:
        d["com.apple.security.application-groups"] = ["group." + bundle]
        changed.append("AppGroup=group." + bundle)
    with open(path, "wb") as fh:
        plistlib.dump(d, fh)
    print("  %s entitlements: %s" % (key, ", ".join(changed) if changed else "无改动"))

# --- WidgetShared：与 entitlements 里的 App Group 保持一致 ---
w = os.environ["WIDGET_SHARED"]
t = open(w, encoding="utf-8").read()
t = t.replace("group.top.btop.music", "group." + bundle)
open(w, "w", encoding="utf-8").write(t)
print("  WidgetShared appGroup -> group.%s" % bundle)

# --- Info.plist：移除 CarPlay scene 声明 ---
if strip_carplay:
    ip = os.environ["INFO"]
    with open(ip, "rb") as fh:
        d = plistlib.load(fh)
    manifest = d.get("UIApplicationSceneManifest", {})
    scenes = manifest.get("UISceneConfigurations", {})
    role = "CPTemplateApplicationSceneSessionRoleApplication"
    if role in scenes:
        scenes.pop(role)
        with open(ip, "wb") as fh:
            plistlib.dump(d, fh)
        print("  Info.plist 已移除 CarPlay scene 声明")
PY

mkdir -p "$OUT"
SPM="$OUT/SourcePackages"
DD="$OUT/DerivedData"
ARCHIVE="$OUT/LemonMusic.xcarchive"

echo "==> 解析 Swift Package 依赖（Vendor/AudioStreaming + ogg/vorbis XCFramework）"
xcodebuild -resolvePackageDependencies \
  -project LemonMusic.xcodeproj \
  -scheme LemonMusic \
  -clonedSourcePackagesDirPath "$SPM"

if [[ -n "$TEAM" ]]; then
  echo "==> 归档并自动签名（Team: $TEAM）"
  XCB_EXTRA=(-allowProvisioningUpdates)
else
  echo "==> 归档（不签名，产出未签名 IPA）"
  XCB_EXTRA=(CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY=)
fi

xcodebuild \
  -project LemonMusic.xcodeproj \
  -scheme LemonMusic \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -derivedDataPath "$DD" \
  -archivePath "$ARCHIVE" \
  -clonedSourcePackagesDirPath "$SPM" \
  "${XCB_EXTRA[@]}" \
  clean archive

APP="$ARCHIVE/Products/Applications/LemonMusic.app"
if [[ ! -d "$APP" ]]; then
  echo "错误：归档中未找到 $APP" >&2
  find "$ARCHIVE/Products" -maxdepth 3 -print >&2 || true
  exit 1
fi

IPA_DIR="$OUT/ipa"
mkdir -p "$IPA_DIR/Payload"
rm -rf "$IPA_DIR/Payload/LemonMusic.app"
cp -R "$APP" "$IPA_DIR/Payload/"
IPA="$OUT/LemonMusic.ipa"
(cd "$IPA_DIR" && zip -qry "$IPA" Payload)

echo "==> 完成"
echo "    APP : $APP"
echo "    IPA : $IPA ($(du -h "$IPA" | cut -f1))"
if [[ -n "$TEAM" ]]; then
  echo "    该 IPA 已签名，可用 AltStore / Sideloadly / Xcode Devices 安装到 iOS 18+ 设备。"
  codesign -d --entitlements :- "$APP" 2>/dev/null | head -40 || true
else
  echo "    该 IPA 未签名，需 TrollStore / 越狱设备，或用 Sideloadly、AltStore、ios-app-signer 配合自己的证书重签。"
fi
echo "    还原原始工程：把 *.bak 文件改回原名即可。"
