//
//  Windows.swift
//  Speaka
//
//  Windows open in the middle of the screen over the dimmed backdrop, the same as Caz's (Caz/Windows.swift), which
//  follow Dopamine's Settings window: DONavigationController, DOPSListController and DOHeaderCell.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import AVKit
import Combine
import SwiftUI
import UIKit

struct WindowCard<Content: View>: View {
    let title: String
    let onBack: (() -> Void)?
    let content: Content

    init(title: String, onBack: (() -> Void)? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.onBack = onBack
        self.content = content()
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Text(title)
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .padding(.horizontal, 50)
                if let onBack {
                    HStack {
                        Button(action: onBack) {
                            Image(systemName: "chevron.left")
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(Theme.foreground)
                                .frame(width: 44, height: 44)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(MenuButtonStyle())
                        .accessibilityLabel("Back")
                        Spacer()
                    }
                    .padding(.leading, 8)
                }
            }
            .frame(height: 57)
            Rectangle()
                .fill(Color.white.opacity(0.1))
                .frame(height: 1)
                .padding(.horizontal, 20)
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.window, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

/// A button like those under Actions in Dopamine's Settings (Refresh Jailbreak Apps, Hide Jailbreak, ...), measured
/// from an iPhone 13 screenshot at 3x: 266 x 36 pt in the 330 pt window, 32 pt in from each side (the window's 16 pt
/// content padding plus `WindowActionButton.inset`); a 1 pt border inside the edge, white at 15%, over no fill; 10 pt
/// circular corners (UIKit's layer radius, not continuous: the traced corner fits a circle to 0.5 px); the symbol at
/// 18 pt and the title at 17 pt Regular, white, 13 pt apart as drawn (10 pt of spacing plus the side bearings),
/// centred. Stacked 8 pt apart.
struct WindowActionButton: View {
    static let height: CGFloat = 36
    static let inset: CGFloat = 16      // beyond the window's 16 pt padding: 32 pt from the window's edges
    static let spacing: CGFloat = 8

    let title: String
    let symbol: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: symbol)
                    .font(.system(size: 18, weight: .regular))
                Text(title)
                    .font(.system(size: 17, weight: .regular))
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
            .frame(maxWidth: .infinity)
            .frame(height: Self.height)
            .overlay(RoundedRectangle(cornerRadius: 10, style: .circular)
                .strokeBorder(Color.white.opacity(0.15), lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: 10, style: .circular))
        }
        .buttonStyle(MenuButtonStyle())
    }
}

/// Where the audio plays: the same three choices as Settings > Speaka > Play audio on, then iOS's own picker for
/// Bluetooth earbuds in Any device mode.
struct OutputWindow: View {
    @ObservedObject var model: SpeakaModel
    @StateObject private var picker = RoutePicker()
    @State private var pickerUnavailable = false

    var body: some View {
        WindowCard(title: "Audio Output") {
            List {
                Section {
                    ForEach(SpeakaModel.outputModes) { mode in
                        OutputRow(mode: mode, selected: mode.key == model.snapshot.outputMode) {
                            model.setOutputMode(mode.key)
                        }
                        .windowRow()
                        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                    }
                } header: {
                    Text("Play audio on")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.white.opacity(0.6))
                } footer: {
                    Text("Changes apply right away, even while audio plays.")
                        .font(.system(size: 12))
                        .foregroundStyle(Color.white.opacity(0.5))
                }

                Section {
                    Button {
                        UIImpactFeedbackGenerator(style: .light).impactOccurred()
                        if !picker.open() {
                            pickerUnavailable = true
                        }
                    } label: {
                        HStack {
                            Text("Choose Output Device")
                                .font(.system(size: 15))
                                .foregroundStyle(.white)
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundStyle(Color.white.opacity(0.6))
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(MenuButtonStyle())
                    .windowRow()
                } footer: {
                    Text("For Bluetooth earbuds: choose Any device, then pick them here or in Control Center.")
                        .font(.system(size: 12))
                        .foregroundStyle(Color.white.opacity(0.5))
                }
            }
            .windowList()
            .background(alignment: .topLeading) {
                RoutePickerAnchor(picker: picker.view)
                    .frame(width: 44, height: 44)
                    .allowsHitTesting(false)
            }
        }
        .alert("Open Control Center", isPresented: $pickerUnavailable) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Press and hold the music controls, then tap the AirPlay icon to choose where audio plays.")
        }
    }
}

/// One choice in Audio Output, drawn like Caz's category rows: the icon in a 22 pt column, the name and what it
/// does, and a checkmark in Caz's gold on the one in use.
struct OutputRow: View {
    let mode: OutputMode
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            action()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: mode.symbol)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Theme.foreground)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 2) {
                    Text(mode.title)
                        .font(.system(size: 16))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                    Text(mode.detail)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.white.opacity(0.55))
                        .lineLimit(2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "checkmark")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                    .opacity(selected ? 1 : 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(MenuButtonStyle())
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// iOS's output picker, opened from a row: an AVRoutePickerView kept almost invisible in the window, whose own
/// button is pressed for you. Settings > Speaka opens it the same way.
final class RoutePicker: ObservableObject {
    let view: AVRoutePickerView = {
        let picker = AVRoutePickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        picker.alpha = 0.02
        picker.isUserInteractionEnabled = false
        return picker
    }()

    /// False when the picker's button can't be found; then Control Center is the way.
    func open() -> Bool {
        view.layoutIfNeeded()
        guard let control = Self.firstControl(in: view) else { return false }
        control.sendActions(for: .touchUpInside)
        return true
    }

    private static func firstControl(in view: UIView) -> UIControl? {
        for sub in view.subviews {
            if let control = sub as? UIControl {
                return control
            }
            if let nested = firstControl(in: sub) {
                return nested
            }
        }
        return nil
    }
}

struct RoutePickerAnchor: UIViewRepresentable {
    let picker: AVRoutePickerView

    func makeUIView(context: Context) -> AVRoutePickerView {
        picker
    }

    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {}
}

/// What Speaka is, which versions are installed, and the license of the interface it shares with Caz.
/// Settings: Kill Bluetooth (the README's `killall -9 bluetoothd`, through the root helper) and Update notifications.
struct SettingsWindow: View {
    @ObservedObject var model: SpeakaModel
    @ObservedObject var updater: SpeakaUpdater
    /// v9.9.2: Credits lives here now (the menu's fourth row is Connect to).
    let showCredits: () -> Void
    /// v9.9.3: Check for Updates found one - the update window, as Update Available opens it.
    let showUpdate: () -> Void

    private var busy: Bool { model.killingBluetooth || model.request != nil }
    private var checking: Bool { updater.manual == .checking }

    var body: some View {
        // v9.9.3: one column of Dopamine's Actions buttons, 266 x 36 pt and 8 pt apart - Kill Bluetooth, Credits, the
        // Update notifications switch in the same frame, and Check for Updates. A line shows under one only when it
        // has something to say, at Caz's 12 pt for footers.
        WindowCard(title: "Settings") {
            VStack(spacing: WindowActionButton.spacing) {
                WindowActionButton(title: model.killingBluetooth ? "Killing Bluetooth..." : "Kill Bluetooth",
                                   symbol: "arrow.triangle.2.circlepath") {
                    model.killBluetooth()
                }
                .disabled(busy)
                .opacity(busy ? 0.5 : 1)
                if let failure = model.killResult {
                    SettingsFootnote(text: failure, failed: true)
                }
                WindowActionButton(title: "Credits", symbol: "info.circle", action: showCredits)
                WindowToggleRow(title: "Update notifications",
                                isOn: Binding(get: { model.updateNotifications },
                                              set: { model.setUpdateNotifications($0) }))
                if model.notificationsRefused {
                    SettingsFootnote(text: "Allow notifications for Speaka in Settings > Notifications, then try again.",
                                     failed: true)
                }
                WindowActionButton(title: checking ? "Checking for Updates..." : "Check for Updates",
                                   symbol: "arrow.down.circle") {
                    updater.checkNow { result in
                        if case .available = result { showUpdate() }
                    }
                }
                .disabled(checking || updater.isBusy)
                .opacity(checking ? 0.5 : 1)
                if let line = checkLine {
                    SettingsFootnote(text: line.text, failed: line.failed)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, WindowActionButton.inset)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .padding(16)
        }
        .onAppear { updater.refreshLastChecked() }
    }

    /// Under Check for Updates: what the last tap found, or else when Speaka (or SpeakaUpdateCheck) last checked.
    private var checkLine: (text: String, failed: Bool)? {
        switch updater.manual {
        case .upToDate(let version):
            return ("Speaka \(version) is up to date.", false)
        case .failed(let reason):
            return ("Couldn't check for updates: \(reason).", true)
        case .idle, .checking:
            guard let when = updater.lastChecked else { return nil }
            let today = Calendar.current.isDateInToday(when)
            let time = DateFormatter.localizedString(from: when, dateStyle: today ? .none : .short, timeStyle: .short)
            return (today ? "Last checked at \(time)." : "Last checked \(time).", false)
        }
    }
}

/// v9.9.3: a switch in WindowActionButton's frame - 266 x 36 pt, the 1 pt border at 15% white, 10 pt circular corners,
/// the title at 17 pt Regular - so it sits with the buttons. The switch at 85% (43 x 26 pt) is 5 pt in from the right,
/// as it is from the top and bottom.
struct WindowToggleRow: View {
    let title: String
    @Binding var isOn: Bool

    var body: some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.system(size: 17, weight: .regular))
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
            Spacer(minLength: 8)
            // A switch is 51 x 31 pt whatever it's asked; scaled, then framed to its new size.
            Toggle(title, isOn: $isOn)
                .labelsHidden()
                .tint(Theme.accent)
                .scaleEffect(0.85)
                .frame(width: 51 * 0.85, height: 31 * 0.85)
        }
        .padding(.leading, 12)
        .padding(.trailing, 5)
        .frame(maxWidth: .infinity)
        .frame(height: WindowActionButton.height)
        .overlay(RoundedRectangle(cornerRadius: 10, style: .circular)
            .strokeBorder(Color.white.opacity(0.15), lineWidth: 1))
    }
}

/// A line under a Settings button: Caz's 12 pt footer, in the warning colour when something failed.
struct SettingsFootnote: View {
    let text: String
    let failed: Bool

    var body: some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(failed ? Theme.failure : Theme.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct CreditsWindow: View {
    /// v9.9.2: opened from Settings, so it has a way back there.
    var onBack: (() -> Void)? = nil

    var body: some View {
        WindowCard(title: "Credits", onBack: onBack) {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Speaka")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(.white)
                    Text("Turns this iPhone into a Bluetooth speaker for your PC.")
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.secondary)
                    Text(versions)
                        .font(.system(size: 12))
                        .foregroundStyle(Color.white.opacity(0.5))
                    // What iOS's Bluetooth list says, which is where the PC's name on the main screen comes from.
                    Text(SPConnectedDevice.lookupSummary())
                        .font(.system(size: 12))
                        .foregroundStyle(Color.white.opacity(0.5))
                        .padding(.bottom, 16)
                    Text("Interface")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(.white)
                    Text("Shared with Caz, and adapted from Dopamine by Lars Fröder (opa334) and its contributors under the MIT License:")
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.secondary)
                        .padding(.bottom, 8)
                    Text(Self.license)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(Color.white.opacity(0.6))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .padding(16)
            }
        }
    }

    /// The app's version and the tweak package's, which a release keeps the same.
    private var versions: String {
        let app = "App \(Bundle.main.shortVersion)"
        guard let package = SpeakaUpdater.installedPackageVersion() else {
            return "\(app) · tweak not installed"
        }
        return "\(app) · tweak \(package)"
    }

    /// Dopamine's MIT License, shipped in the app (app/Resources/LICENSE-Dopamine.txt).
    static let license: String = {
        if let path = Bundle.main.path(forResource: "LICENSE-Dopamine", ofType: "txt"),
           let text = try? String(contentsOfFile: path, encoding: .utf8), !text.isEmpty {
            return text
        }
        return "MIT License\n\nCopyright (c) 2023-2024 Lars Fröder (opa334)"
    }()
}

/// v9.9.2: the menu's fourth row - where connecting to a computer will live. A window of its own, empty for now.
struct ConnectWindow: View {
    var body: some View {
        WindowCard(title: "Connect to") {
            Text("Computers you can connect to will appear here.")
                .font(.system(size: 15))
                .foregroundStyle(Theme.secondary)
                .multilineTextAlignment(.center)
                .padding(16)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
