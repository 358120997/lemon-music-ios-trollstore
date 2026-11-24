#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iOS 17 兼容守卫：确保源码树里不含 iOS 18+/26+ 才有的 SwiftUI / CarPlay API。

背景：原工程部署目标 18.0，MainTabView 裸用 iOS 18 的 Tab(_:systemImage:value:)
以及 iOS 26 的 .tabViewBottomAccessory / tabViewBottomAccessoryPlacement；
CarPlayCoordinator 用了 iOS 18.4 的 showsSpinnerWhileEmpty。
把部署目标降到 17.1 后，这些符号要么触发 "only available in iOS xx" 编译错误，
要么在低版本 SDK 里根本不存在（#available 只保护运行时，不解决编译期符号缺失）。

本脚本做两件事：
  1. 检测上述高版本 API；若源码被上游还原，就地替换为 iOS 17 等价实现。
  2. 无论是否改动，最后做一次静态校验，残留则失败退出（避免白跑一次 CI）。

幂等：源码已经是 iOS 17 版时全部跳过，只打印 OK。
修改前备份到 build/trollstore/backup/。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKUP = ROOT / "build" / "trollstore" / "backup"

MAINTAB = ROOT / "LemonMusic" / "UI" / "MainTabView.swift"
CARPLAY = ROOT / "LemonMusic" / "CarPlay" / "CarPlayCoordinator.swift"
COLLECTION = ROOT / "LemonMusic" / "UI" / "CollectionDetailView.swift"
WIDGETS = ROOT / "LemonMusicWidgets" / "LemonMusicWidgets.swift"
PLAYER = ROOT / "LemonMusic" / "UI" / "PlayerViews.swift"
PBX = ROOT / "LemonMusic.xcodeproj" / "project.pbxproj"

# 不含任何 iOS 18+ 符号的完整替代实现。
IOS17_MAINTAB = '''// Derived from Cassette, licensed under MPL-2.0.

import SwiftUI

// iOS 17 兼容版（部署目标 17.1）：
//  - 不使用 iOS 18 的 Tab(_:systemImage:value:) / Tab(value:role:)，
//    改用 iOS 13 起就有的 TabView + tabItem + tag。
//  - 不使用 iOS 26 的 .tabViewBottomAccessory 与
//    @Environment(\\.tabViewBottomAccessoryPlacement)，mini player 统一走
//    LegacyMiniPlayerLayout 的 VStack 底部布局，视觉与 iOS 26 之前的版本一致。
//  - 因此整个文件不含任何 iOS 18+ 符号，Xcode 16 / Xcode 26 的 SDK 都能编译。

struct MainTabView: View {
    @Environment(AppModel.self) private var model
    @State private var selectedTab: AppTab = .home
    private var showingPlayer: Bool { model.playback.showingFullPlayer }
    private var playerPresentation: Binding<Bool> {
        Binding(get: { model.playback.showingFullPlayer }, set: { model.playback.showingFullPlayer = $0 })
    }

    private enum AppTab: Hashable { case home, discover, library, search }

    var body: some View {
        tabs
            .fullScreenCover(isPresented: playerPresentation) {
                FullPlayerView()
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                if let error = model.userDataSyncError {
                    HStack(spacing: 8) {
                        Label("数据同步失败", systemImage: "exclamationmark.circle")
                        Text(error).lineLimit(1).foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Button {
                            Task { await model.retryUserDataSync() }
                        } label: {
                            Label("重试同步", systemImage: "arrow.clockwise")
                        }
                        .labelStyle(.iconOnly)
                        .frame(width: 44, height: 44)
                    }
                    .font(.caption)
                    .padding(.horizontal)
                    .background(Color(.secondarySystemBackground))
                }
            }
            .overlay(alignment: .top) {
                if model.sessionState == .offline {
                    Text("离线模式")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(.orange, in: Capsule())
                        .foregroundStyle(.white)
                        .padding(.top, 8)
                }
            }
            .alert("无法播放", isPresented: Binding(
                get: { !showingPlayer && model.playback.lastError != nil },
                set: { if !$0 && !showingPlayer { model.playback.lastError = nil } }
            )) {
                Button("知道了", role: .cancel) { model.playback.lastError = nil }
            } message: {
                Text(model.playback.lastError ?? "请稍后重试")
            }
    }

    private var tabs: some View {
        TabView(selection: $selectedTab) {
            NavigationStack { HomeView() }
                .modifier(LegacyMiniPlayerLayout(showingPlayer: playerPresentation))
                .tabItem { Label("首页", systemImage: "house.fill") }
                .tag(AppTab.home)
            NavigationStack { DiscoverView() }
                .modifier(LegacyMiniPlayerLayout(showingPlayer: playerPresentation))
                .tabItem { Label("发现", systemImage: "sparkles") }
                .tag(AppTab.discover)
            NavigationStack { LibraryView() }
                .modifier(LegacyMiniPlayerLayout(showingPlayer: playerPresentation))
                .tabItem { Label("音乐库", systemImage: "music.note.list") }
                .tag(AppTab.library)
            NavigationStack { SearchView() }
                .modifier(LegacyMiniPlayerLayout(showingPlayer: playerPresentation))
                .tabItem { Label("搜索", systemImage: "magnifyingglass") }
                .tag(AppTab.search)
        }
    }
}

private struct LegacyMiniPlayerLayout: ViewModifier {
    @Environment(AppModel.self) private var model
    @Binding var showingPlayer: Bool

    func body(content: Content) -> some View {
        VStack(spacing: 0) {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            if model.playback.currentTrack != nil {
                MiniPlayerView(showingPlayer: $showingPlayer, presentation: .legacy)
                    .padding(.horizontal, 10)
                    .padding(.top, 6)
                    .padding(.bottom, 4)
            }
        }
    }
}
'''

CARPLAY_NOTE = ("            // iOS 17 兼容：showsSpinnerWhileEmpty 是 iOS 18.4 SDK 才有的属性，\n"
                "            // 直接引用会让低版本 SDK 编译失败；空列表的文案已足够表达加载状态。\n")

# 编译期会出错的高版本符号（注释里出现不算，故只扫代码形态）。
FORBIDDEN = [
    (r"\bTab\s*\(", "iOS 18 的 SwiftUI Tab(...)"),
    (r"\.tabViewBottomAccessory\b", "iOS 26 的 .tabViewBottomAccessory"),
    (r"tabViewBottomAccessoryPlacement", "iOS 26 的 tabViewBottomAccessoryPlacement"),
    (r"showsSpinnerWhileEmpty", "iOS 18.4 的 showsSpinnerWhileEmpty"),
    (r"\.widgetAccentedRenderingMode\b", "iOS 18.0 的 widgetAccentedRenderingMode"),
    (r"\.onGeometryChange\b", "iOS 18.0 的 onGeometryChange"),
    (r"\.help\(", "SwiftUI .help(...) 修饰符"),
]


def backup(path: pathlib.Path) -> None:
    BACKUP.mkdir(parents=True, exist_ok=True)
    (BACKUP / (path.name + ".bak")).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def code_has(text: str, pattern: str) -> bool:
    """只在非注释代码行里找符号；说明文字提到 API 名不算残留。"""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if re.search(pattern, stripped):
            return True
    return False


def patch_maintab() -> str:
    s = MAINTAB.read_text(encoding="utf-8")
    dirty = any(code_has(s, p) for p, _ in FORBIDDEN[:3])
    if not dirty:
        return "MainTabView: 已是 iOS 17 版，跳过"
    backup(MAINTAB)
    MAINTAB.write_text(IOS17_MAINTAB, encoding="utf-8")
    return "MainTabView: 已整体替换为 iOS 17 版（tabItem/tag + legacy mini player）"


def patch_carplay() -> str:
    if not CARPLAY.exists():
        return "CarPlayCoordinator: 文件不存在（无 CarPlay 模块），跳过"
    s = CARPLAY.read_text(encoding="utf-8")
    if not re.search(r"if #available\(iOS 18\.4, \*\) \{.*showsSpinnerWhileEmpty.*\}", s):
        return "CarPlayCoordinator: 已兼容，跳过"
    backup(CARPLAY)
    s = re.sub(r"^.*if #available\(iOS 18\.4, \*\) \{.*showsSpinnerWhileEmpty.*\}\n",
               CARPLAY_NOTE, s, flags=re.M)
    CARPLAY.write_text(s, encoding="utf-8")
    return "CarPlayCoordinator: 已移除 iOS 18.4 spinner 属性"


def patch_help() -> str:
    if not COLLECTION.exists():
        return "CollectionDetailView: 文件不存在，跳过"
    s = COLLECTION.read_text(encoding="utf-8")
    if ".help(" not in s:
        return "CollectionDetailView: 已兼容，跳过"
    backup(COLLECTION)
    s = re.sub(r"\.help\([^)]*\)", "", s)
    COLLECTION.write_text(s, encoding="utf-8")
    return "CollectionDetailView: 已移除 .help(...) 修饰符"


def patch_widget() -> str:
    if not WIDGETS.exists():
        return "LemonMusicWidgets: 文件不存在，跳过"
    s = WIDGETS.read_text(encoding="utf-8")
    if ".widgetAccentedRenderingMode" not in s:
        return "LemonMusicWidgets: 已兼容，跳过"
    backup(WIDGETS)
    s = re.sub(r"^([^\n]*)\.widgetAccentedRenderingMode\([^)]*\)([^\n]*)$",
               lambda m: m.group(1) + m.group(2), s, flags=re.M)
    WIDGETS.write_text(s, encoding="utf-8")
    return "LemonMusicWidgets: 已移除 iOS 18 的 widgetAccentedRenderingMode"


GEO_OLD = "        .onGeometryChange(for: CGRect.self, of: { $0.frame(in: .global) }) { progressFrame = $0 }\n"
GEO_NEW = """        .background {
            GeometryReader { proxy in
                Color.clear.preference(key: ProgressFramePreferenceKey.self,
                                       value: proxy.frame(in: .global))
            }
        }
        .onPreferenceChange(ProgressFramePreferenceKey.self) { progressFrame = $0 }
"""
GEO_KEY = """private struct ProgressFramePreferenceKey: PreferenceKey {
    static let defaultValue: CGRect = .zero
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        value = nextValue()
    }
}

"""


def patch_player() -> str:
    if not PLAYER.exists():
        return "PlayerViews: 文件不存在，跳过"
    s = PLAYER.read_text(encoding="utf-8")
    if not code_has(s, r"\.onGeometryChange\b"):
        return "PlayerViews: 已兼容，跳过"
    backup(PLAYER)
    if "ProgressFramePreferenceKey" not in s:
        s = s.replace("import SwiftUI\n\n", "import SwiftUI\n\n" + GEO_KEY, 1)
    s = s.replace(GEO_OLD, GEO_NEW, 1)
    PLAYER.write_text(s, encoding="utf-8")
    return "PlayerViews: onGeometryChange -> GeometryReader + PreferenceKey"


def patch_pbx() -> str:
    s = PBX.read_text(encoding="utf-8")
    if "IPHONEOS_DEPLOYMENT_TARGET = 18.0;" not in s:
        return "project.pbxproj: 部署目标已 <= 17.x，跳过"
    backup(PBX)
    s = s.replace("IPHONEOS_DEPLOYMENT_TARGET = 18.0;",
                  "IPHONEOS_DEPLOYMENT_TARGET = 17.1;")
    PBX.write_text(s, encoding="utf-8")
    return "project.pbxproj: 部署目标 18.0 -> 17.1"


def verify() -> list[str]:
    problems = []
    for base in (ROOT / "LemonMusic", ROOT / "LemonMusicWidgets", ROOT / "WidgetShared"):
        for path in base.rglob("*.swift"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern, label in FORBIDDEN:
                for m in re.finditer(pattern, text):
                    line = text[:m.start()].count("\n") + 1
                    # 注释里的说明文字不算
                    if text.splitlines()[line - 1].lstrip().startswith("//"):
                        continue
                    problems.append("%s:%d 残留 %s" % (path.relative_to(ROOT), line, label))
    return problems


def main() -> int:
    for fn in (patch_maintab, patch_carplay, patch_help, patch_widget, patch_player, patch_pbx):
        print("ios17_compat:", fn())
    problems = verify()
    if problems:
        print("iOS 17 兼容校验失败，仍有高版本 API:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    print("ios17_compat: 静态校验通过（无 iOS 18+/26+ API 残留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
