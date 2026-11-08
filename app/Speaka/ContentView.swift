//
//  ContentView.swift
//  Speaka
//
//  The main screen, laid out exactly like Caz's (Caz/ContentView.swift), which follows Dopamine's
//  DOMainViewController: a header, the action menu and the big button in one vertical stack, 73% of the screen tall
//  with 30 pt margins. The big button is Start or Stop; tapping it opens it into the log, as Caz's Clean button does.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import Combine
import SwiftUI
import UIKit

enum SpeakaWindow: Equatable {
    case settings, output, credits, connect, update
}

struct ContentView: View {
    @StateObject private var model = SpeakaModel()
    @StateObject private var updater = SpeakaUpdater()
    @State private var window: SpeakaWindow? = nil
    @Environment(\.scenePhase) private var scenePhase
    @Namespace private var actionSpace

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Backdrop(dimmed: window != nil, size: geo.size)
                mainStack(size: geo.size)
                if model.sheetVisible {
                    expandedSheet(size: geo.size)
                }
                if let window {
                    windowLayer(window, size: geo.size)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
        .ignoresSafeArea()
        .animation(.spring(response: 0.75, dampingFraction: 0.9), value: model.sheetVisible)
        .animation(.spring(response: 0.6, dampingFraction: 0.9), value: window)
        // Update Available and the connection line come and go like Caz's Update Available: up 25 pt as they fade in.
        .animation(.spring(response: 0.5, dampingFraction: 0.9), value: updater.showsLink)
        .animation(.spring(response: 0.5, dampingFraction: 0.9), value: model.statusLine != nil)
        // A light tap as each step finishes and a success buzz at the end, as in Caz. UIKit's generators,
        // because sensoryFeedback needs iOS 17.
        .onChange(of: model.completedSteps) { steps in
            if steps > 0 { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
        }
        .onChange(of: model.successCount) { count in
            if count > 0 { UINotificationFeedbackGenerator().notificationOccurred(.success) }
        }
        .onChange(of: scenePhase) { phase in
            model.setActive(phase == .active)
            if phase == .active {
                updater.refreshTweak()   // installed from Sileo meanwhile, say
                // v9.9.3: at once, so the Update Available a notification promised is there when Speaka opens.
                updater.checkOnActivation()
                model.syncNotificationPermission()
            }
            // Update notifications: every 2 minutes while open; in the background, when iOS chooses.
            updater.watch(model.updateNotifications && phase == .active)
            if phase == .background && model.updateNotifications {
                UpdateNotifications.schedule()
            }
        }
        .onChange(of: model.updateNotifications) { on in
            updater.watch(on && scenePhase == .active)
        }
        .onAppear {
            model.setActive(true)
            updater.check()
            updater.watch(model.updateNotifications)
            model.syncNotificationPermission()
            if SpeakaNotificationDelegate.openRequested { openUpdateWhenReady() }   // the tap launched Speaka
        }
        // v9.9.3: a tap on an update notification opens the update window.
        .onReceive(NotificationCenter.default.publisher(for: SpeakaNotificationDelegate.openUpdate)) { _ in
            openUpdateWhenReady()
        }
    }

    /// A fresh check, then the update window if there's an update (what the notification was about may be installed
    /// by now, or not published with the app after all: then nothing opens).
    private func openUpdateWhenReady() {
        SpeakaNotificationDelegate.openRequested = false
        guard !model.uninstalling else { return }
        updater.check(again: true) { result in
            if case .available = result, !updater.isBusy, !model.uninstalling {
                window = .update
            }
        }
    }

    // MARK: - Main stack

    private func mainStack(size: CGSize) -> some View {
        let expanded = model.sheetVisible
        let windowOpen = window != nil
        let small = Metrics.isSmall(size.height)
        let stackHeight = size.height * (Metrics.isHomeButtonDevice ? 0.78 : 0.73)
        let stackWidth = Metrics.isPad ? min(size.width * 0.8, Metrics.iPadMaxWidth) : size.width - Metrics.padding * 2
        let statusOffset = max(15, Metrics.safeTop - 20)
        let linkGap: CGFloat = Metrics.isHomeButtonDevice ? 10 : 20

        return VStack(spacing: 0) {
            HeaderView(subtitle: model.headerSubtitle,
                       detail: model.headerDetail,
                       detailIsWarning: model.headerDetailIsWarning,
                       secondWarning: model.headerSecondWarning)
                .offset(y: expanded ? -25 : 0)
            Spacer(minLength: 12)
            ActionMenu(items: menuItems,
                       rowHeight: Metrics.actionHeight(size.height),
                       innerPadding: small ? Metrics.innerPaddingTiny : Metrics.innerPadding)
                .scaleEffect(expanded ? 0.6 : 1)
                .offset(y: expanded ? -75 : 0)
                .opacity(expanded ? 0 : 1)
                .allowsHitTesting(!expanded)
            Spacer(minLength: 12)
            ZStack {
                if !expanded {
                    ActionButton(title: model.buttonTitle,
                                 symbol: model.buttonSymbol,
                                 enabled: model.canPress) {
                        model.toggle()
                    }
                    .accessibilityHint("Bluetooth restarts once.")
                    .matchedGeometryEffect(id: "action", in: actionSpace)
                }
            }
            .frame(height: 60)
            .overlay(alignment: .top) {
                // Caz's Update Available: 30 pt tall, 20 pt above the big button (10 on home-button phones), sliding
                // up 25 pt as it fades in, and hidden while the big button is open. The connection line takes the same
                // place, or the line above it, 10 pt higher, while Update Available is showing.
                // v9.5: with no tweak installed it's Install Tweak, which installs the one this app carries; with a
                // tweak installed, of whatever version, it's Update Available as before.
                if updater.showsLink && !model.uninstalling {        // no Install Tweak while it's being removed
                    UpdateLinkButton(title: updater.tweakMissing ? "Install Tweak" : "Update Available",
                                     symbol: "arrow.down.circle") {
                        if updater.tweakMissing {
                            updater.installTweak()
                        }
                        window = .update
                    }
                    .frame(height: 30)
                    .offset(y: -(30 + linkGap))
                    .opacity(expanded ? 0 : 1)
                    .allowsHitTesting(!expanded)
                    .transition(.offset(y: 25).combined(with: .opacity))
                }
                if let line = model.statusLine {
                    StatusLineView(line: line)
                        .frame(height: 30)
                        .offset(y: -(30 + linkGap) - (updater.showsLink ? 40 : 0))
                        .opacity(expanded ? 0 : 1)
                        .transition(.offset(y: 25).combined(with: .opacity))
                }
            }
        }
        .frame(width: stackWidth, height: stackHeight)
        .position(x: size.width / 2, y: size.height / 2 + statusOffset)
        .scaleEffect(windowOpen ? 0.9 : 1)
        .opacity(windowOpen ? 0 : 1)
        .allowsHitTesting(!windowOpen)
    }

    // MARK: - Expanded button (the log)

    private func expandedSheet(size: CGSize) -> some View {
        let top = size.height * 0.26 + 35
        let visible = size.height - top
        return LogSheet(model: model, visibleHeight: visible)
            .matchedGeometryEffect(id: "action", in: actionSpace)
            .frame(width: size.width, height: visible + 100)
            .position(x: size.width / 2, y: top + (visible + 100) / 2)
            .zIndex(1)
    }

    // MARK: - Windows

    private func windowLayer(_ window: SpeakaWindow, size: CGSize) -> some View {
        let small = Metrics.isSmall(size.height)
        return ZStack {
            Color.clear
                .contentShape(Rectangle())
                .onTapGesture {
                    if window == .update && updater.isBusy { return }   // can't back out mid-update
                    self.window = nil
                    updater.resetIfIdle()
                }
            windowView(window, size: size)
                .frame(width: min(size.width - Metrics.modalPadding * 2, Metrics.iPadMaxWidth),
                       height: size.height * (small ? 0.8 : 0.7))
                .transition(.scale(scale: 0.7).combined(with: .opacity))
        }
        .zIndex(2)
    }

    @ViewBuilder
    private func windowView(_ window: SpeakaWindow, size: CGSize) -> some View {
        switch window {
        case .settings:
            SettingsWindow(model: model, updater: updater,
                           showCredits: { self.window = .credits },   // v9.9.2: Credits moved here
                           showUpdate: { self.window = .update })     // v9.9.3: Check for Updates found one
        case .output:
            OutputWindow(model: model)
        case .credits:
            CreditsWindow { self.window = .settings }
        case .connect:
            ConnectWindow(model: model)
        case .update:
            UpdateFlowView(updater: updater, screenWidth: size.width)
        }
    }

    // MARK: - Menu

    /// Four rows, like Caz's menu, so the panel is Caz's to the point: 4 x 73 + 10 = 302 pt at y 306.7 on an iPhone 13.
    /// Uninstall is an action, like Caz's Scan, so no chevron. Once it starts, every other row is locked.
    private var menuItems: [MenuItem] {
        let locked = model.uninstalling
        return [
            MenuItem(id: "settings", title: "Settings", symbol: "gearshape",
                     chevron: true, enabled: !locked) { window = .settings },
            MenuItem(id: "output", title: "Audio Output", symbol: "speaker.wave.2",
                     chevron: true, enabled: !locked) { window = .output },
            MenuItem(id: "uninstall", title: "Uninstall", symbol: "trash",
                     enabled: model.request == nil && !updater.isBusy,
                     holdSeconds: 4, busy: locked) { model.uninstall() },
            // v9.9.2: Connect to, where Credits was (Credits is in Settings now).
            MenuItem(id: "connect", title: "Connect to", symbol: "link",
                     chevron: true, enabled: !locked) { window = .connect },
        ]
    }
}
