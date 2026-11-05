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

    private var busy: Bool { model.killingBluetooth || model.request != nil }

    var body: some View {
        WindowCard(title: "Settings") {
            VStack(spacing: 12) {
                Button {
                    model.killBluetooth()
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "arrow.triangle.2.circlepath")
                        Text(model.killingBluetooth ? "Killing Bluetooth..." : "Kill Bluetooth")
                    }
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 50)
                    .background(Theme.menu, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .opacity(busy ? 0.5 : 1)
                if let failure = model.killResult {
                    Text(failure)
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.failure)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Toggle(isOn: Binding(get: { model.updateNotifications },
                                     set: { model.setUpdateNotifications($0) })) {
                    Text("Update notifications")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(.white)
                }
                .tint(Theme.accent)
                .padding(.horizontal, 16)
                .frame(height: 50)
                .background(Theme.menu, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                Text(model.notificationsRefused
                     ? "Allow notifications for Speaka in Settings > Notifications, then try again."
                     : "Every 2 minutes while Speaka is open; in the background, as often as iOS allows.")
                    .font(.system(size: 13))
                    .foregroundStyle(model.notificationsRefused ? Theme.failure : Color.white.opacity(0.5))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .padding(16)
        }
    }
}

struct CreditsWindow: View {

    var body: some View {
        WindowCard(title: "Credits") {
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
