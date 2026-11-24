// Derived from Cassette, licensed under MPL-2.0.

import SwiftUI

// iOS 17 兼容版（部署目标 17.1）：
//  - 不使用 iOS 18 的 Tab(_:systemImage:value:) / Tab(value:role:)，
//    改用 iOS 13 起就有的 TabView + tabItem + tag。
//  - 不使用 iOS 26 的 .tabViewBottomAccessory 与
//    @Environment(\.tabViewBottomAccessoryPlacement)，mini player 统一走
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
