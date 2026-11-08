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
    /// v9.9.4: filled, with its edge in the same colour (Update notifications when on); none is Dopamine's outline.
    var fill: Color? = nil
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
            .background(RoundedRectangle(cornerRadius: 10, style: .circular).fill(fill ?? .clear))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .circular)
                .strokeBorder(fill ?? Color.white.opacity(0.15), lineWidth: 1))
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
        // v9.9.3: one column of Dopamine's Actions buttons, 266 x 36 pt and 8 pt apart - Kill Bluetooth, Credits,
        // Update notifications (v9.9.4: a button too, green when on) and Check for Updates. A line shows under one
        // only when it has something to say, at Caz's 12 pt for footers.
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
                // v9.9.4: a button like the others, centred: a bell when off; iOS's green with a checkmark when on.
                // A tap toggles it, as the switch did (turning it on asks iOS for permission first).
                WindowActionButton(title: "Update notifications",
                                   symbol: model.updateNotifications ? "checkmark" : "bell",
                                   fill: model.updateNotifications ? Theme.enabled : nil) {
                    model.setUpdateNotifications(!model.updateNotifications)
                }
                .accessibilityValue(model.updateNotifications ? "On" : "Off")
                .animation(.easeOut(duration: 0.2), value: model.updateNotifications)
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

/// v9.9.12: Connect to - the computers that connected to Speaka, the devices nearby, and asking iOS to connect one.
/// Connect links the iPhone and the computer over Bluetooth; audio into Speaka still starts on the computer, which
/// chooses its output, so the page says so rather than claiming more.
struct ConnectWindow: View {
    @ObservedObject var model: SpeakaModel
    @StateObject private var connect = ConnectModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        WindowCard(title: "Connect to") {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .onAppear {
            connect.open(streamAddress: model.streamAddress, streaming: model.streamConnected, playing: model.streamPlaying)
        }
        .onDisappear { connect.close() }
        .onChange(of: scenePhase) { phase in
            if phase == .active {
                connect.open(streamAddress: model.streamAddress, streaming: model.streamConnected, playing: model.streamPlaying)
            } else {
                connect.close()
            }
        }
        .onChange(of: model.streamAddress) { _ in
            connect.update(streamAddress: model.streamAddress, streaming: model.streamConnected, playing: model.streamPlaying)
        }
        .onChange(of: model.streamPlaying) { _ in
            connect.update(streamAddress: model.streamAddress, streaming: model.streamConnected, playing: model.streamPlaying)
        }
    }

    /// The list always shows: Speaka's history and its stream don't need BluetoothManager, so they stay when Bluetooth
    /// is off or not available to Speaka; only scanning and Connect wait for it (SPBluetoothDevices).
    private var content: some View { list }

    @ViewBuilder
    private var bluetoothState: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch connect.availability {
            case .starting:
                HStack(spacing: 8) {
                    ProgressView().tint(.white)
                    Text("Waiting for Bluetooth...")
                        .font(.system(size: 15))
                        .foregroundStyle(Theme.secondary)
                }
            case .off:
                Text("Bluetooth is off")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.white)
                Text("Turn Bluetooth on to find and connect your computers.")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                WindowActionButton(title: "Bluetooth Settings", symbol: "gearshape") { ConnectWindow.openBluetoothSettings() }
                    .padding(.top, 6)
            default:
                Text("Bluetooth isn't available to Speaka")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.white)
                Text(connect.detail)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 14)
    }

    /// iOS's Bluetooth page, else Speaka's own page in Settings. canOpenURL is false for any scheme not in
    /// LSApplicationQueriesSchemes, so this asks open itself.
    static func openBluetoothSettings() {
        guard let bluetooth = URL(string: "App-prefs:Bluetooth") else { return }
        UIApplication.shared.open(bluetooth, options: [:]) { opened in
            if !opened, let own = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(own)
            }
        }
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if connect.availability != .ready {
                    bluetoothState
                }
                ConnectSectionHeader(title: "Recently connected")
                section(.recent, empty: "Computers that send audio to Speaka show here after they connect.")
                ConnectSectionHeader(title: "Nearby devices") {
                    if connect.scanning {
                        HStack(spacing: 6) {
                            ProgressView().tint(Theme.secondary).scaleEffect(0.7)
                            Text("Searching")
                                .font(.system(size: 13))
                                .foregroundStyle(Theme.secondary)
                        }
                    } else if connect.canScan {
                        Button("Rescan") { connect.rescan() }
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(Theme.accent)
                            .buttonStyle(MenuButtonStyle())
                    }
                }
                section(.nearby, empty: connect.availability != .ready ? "Searching needs Bluetooth."
                        : connect.scanning ? "Searching for computers nearby..."
                        : "No computers found. On the computer, open Bluetooth settings so it can be found, then Rescan.")
                if !connect.rows(in: .paired).isEmpty {
                    ConnectSectionHeader(title: "Other paired devices")
                    section(.paired, empty: "")
                }
                VStack(alignment: .leading, spacing: 8) {
                    if let notice = connect.notice {
                        SettingsFootnote(text: notice, failed: false)
                    }
                    if connect.heldForAudio && !connect.scanning {
                        SettingsFootnote(text: "Speaka is playing, so it didn't scan: a scan can interrupt Bluetooth audio. "
                                         + "Rescan to scan anyway.", failed: false)
                    }
                    SettingsFootnote(text: "Connect links this iPhone and the computer and lets the computer connect its "
                                     + "audio to Speaka. If it connects without sound, choose Speaka as the output on "
                                     + "the computer.", failed: false)
                }
                .padding(.top, 14)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
    }

    @ViewBuilder
    private func section(_ section: SPDeviceSection, empty: String) -> some View {
        let rows = connect.rows(in: section)
        if rows.isEmpty {
            if !empty.isEmpty {
                Text(empty)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 8)
            }
        } else {
            ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                if index > 0 {
                    Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1).padding(.leading, 40)
                }
                ConnectRowView(row: row) { connect.connect(row) }
            }
        }
    }
}

/// A section's title, like Dopamine Settings' (JAILBREAK SETTINGS, ACTIONS), with an optional control at the right.
struct ConnectSectionHeader<Trailing: View>: View {
    let title: String
    let trailing: Trailing

    init(title: String, @ViewBuilder trailing: () -> Trailing) {
        self.title = title
        self.trailing = trailing()
    }

    var body: some View {
        HStack {
            Text(title)
                .font(.system(size: 13))
                .textCase(.uppercase)
                .foregroundStyle(Theme.secondary)
            Spacer(minLength: 8)
            trailing
        }
        .padding(.top, 16)
        .padding(.bottom, 4)
    }
}

extension ConnectSectionHeader where Trailing == EmptyView {
    init(title: String) {
        self.init(title: title) { EmptyView() }
    }
}

/// One device: its icon, its name, what it's doing, and Connect when iOS can be asked.
struct ConnectRowView: View {
    let row: ConnectModel.Row
    let connect: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 20, weight: .regular))
                .foregroundStyle(.white)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name)
                    .font(.system(size: 17))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(status)
                    .font(.system(size: 12))
                    .foregroundStyle(row.failure != nil ? Theme.failure : Theme.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            trailing
        }
        .frame(minHeight: 56)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private var symbol: String {
        switch row.kind {
        case .computer: return row.name.uppercased().hasPrefix("DESKTOP-") ? "desktopcomputer" : "laptopcomputer"
        case .phone: return "iphone"
        default: return "dot.radiowaves.left.and.right"
        }
    }

    @ViewBuilder
    private var trailing: some View {
        switch row.state {
        case .connected:
            Image(systemName: "checkmark")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(Theme.enabled)
                .accessibilityLabel("Connected")
        case .connecting:
            ProgressView().tint(.white)
        default:
            if row.canConnect {
                Button(action: connect) {
                    Text("Connect")
                        .font(.system(size: 15, weight: .regular))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 12)
                        .frame(height: 32)
                        .overlay(RoundedRectangle(cornerRadius: 10, style: .circular)
                            .strokeBorder(Color.white.opacity(0.15), lineWidth: 1))
                        .contentShape(Rectangle())
                }
                .buttonStyle(MenuButtonStyle())
            }
        }
    }

    private var status: String {
        if let failure = row.failure { return "Couldn't connect: \(failure)" }
        let last = row.lastConnected.map { " · last connected \(ConnectRowView.when($0))" } ?? ""
        switch row.state {
        case .connected: return "Connected"
        case .connecting: return "Connecting..."
        case .linked: return "Connected to this iPhone · choose Speaka as its output"
        case .available: return row.paired ? "Available" + last : "Available · pair it from the computer"
        case .paired: return "Paired" + last
        default: return "Not available" + last
        }
    }

    static func when(_ date: Date) -> String {
        let calendar = Calendar.current
        let time = DateFormatter.localizedString(from: date, dateStyle: .none, timeStyle: .short)
        if calendar.isDateInToday(date) { return "today at \(time)" }
        if calendar.isDateInYesterday(date) { return "yesterday at \(time)" }
        return DateFormatter.localizedString(from: date, dateStyle: .medium, timeStyle: .none)
    }
}
