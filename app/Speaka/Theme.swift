//
//  Theme.swift
//  Speaka
//
//  Colours, metrics and small building blocks, the same as Caz's (Caz/Theme.swift) so the two apps look alike.
//  Caz follows Dopamine's DOGlobalAppearance, DOThemeManager and Themes.plist, with the yellow theme of its icon.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import CoreText
import SwiftUI
import UIKit

enum Theme {
    /// The menu and the big button. Dopamine's default theme sets actionMenuColor to 723f3f3f, read as
    /// ARGB: #3F3F3F at 114/255 (44.7%). DOActionMenuView and DOJailbreakButton both use it as is.
    static let menu = Color(red: 63 / 255.0, green: 63 / 255.0, blue: 63 / 255.0).opacity(114 / 255.0)
    /// The Audio Output and Credits windows. Dopamine's windowColor 994c4c4c: #4C4C4C at 153/255 (60%).
    static let window = Color(red: 76 / 255.0, green: 76 / 255.0, blue: 76 / 255.0).opacity(153 / 255.0)
    /// Caz's gold, used for the checkmark of the audio output in use.
    static let accent = Color(red: 255 / 255.0, green: 178 / 255.0, blue: 30 / 255.0)
    static let separator = Color(white: 0.6).opacity(0.3)
    /// Menu and button titles and icons. Dopamine's button configuration only dims white given in an
    /// RGB colour space, so plain white renders at full strength (measured at 253, 254, 254).
    static let text = Color.white
    /// Secondary icons inside windows.
    static let foreground = Color.white.opacity(0.75)
    static let secondary = Color.white.opacity(0.8)
    static let failure = Color(red: 1.0, green: 0.62, blue: 0.56)
}

enum Metrics {
    static let padding: CGFloat = 30            // UI_PADDING
    static let innerPadding: CGFloat = 20       // UI_INNER_PADDING
    static let innerPaddingTiny: CGFloat = 10   // UI_INNER_PADDING_TINY
    static let innerTopPadding: CGFloat = 5     // UI_INNER_TOP_PADDING
    static let modalPadding: CGFloat = 30       // UI_MODAL_PADDING
    static let iPadMaxWidth: CGFloat = 600      // UI_IPAD_MAX_WIDTH

    static var keyWindow: UIWindow? {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }
            .first { $0.isKeyWindow }
    }

    static var safeTop: CGFloat { keyWindow?.safeAreaInsets.top ?? 47 }

    static var isHomeButtonDevice: Bool {
        UIDevice.current.userInterfaceIdiom == .phone && (keyWindow?.safeAreaInsets.bottom ?? 34) == 0
    }

    static var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    static func isSmall(_ height: CGFloat) -> Bool { height < 568 + 50 }

    /// UI_ACTION_HEIGHT, UI_ACTION_HEIGHT_HOME_BTN, UI_ACTION_HEIGHT_TINY
    static func actionHeight(_ height: CGFloat) -> CGFloat {
        if isSmall(height) { return 52 }
        return isHomeButtonDevice ? 65 : 73
    }
}

/// A plain button that dims while pressed, like UIButtonConfiguration.plain.
struct MenuButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.45 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

/// Caz's yellow backdrop (Sunshine Sweep). It dims to 40% while a window is open, like Dopamine's.
struct Backdrop: View {
    var dimmed: Bool

    var body: some View {
        Color.black
            .overlay {
                Image("Backdrop")
                    .resizable()
                    .scaledToFill()
                    .opacity(dimmed ? 0.4 : 1)
                    .animation(.easeInOut(duration: 0.3), value: dimmed)
            }
            .clipped()
            .ignoresSafeArea()
    }
}

extension View {
    /// Rows in a window: no cell background and no separators, as in Dopamine's lists.
    func windowRow() -> some View {
        self
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
    }

    func windowList() -> some View {
        self
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .environment(\.defaultMinListRowHeight, 48)
            .tint(Theme.accent)
    }
}

/// The wordmark's font, used only while Assets.xcassets has no Wordmark picture. Any .ttf or .otf in the app
/// (app/Resources) is registered at launch and the first one is used. Without one, SF Rounded Bold is used.
enum WordmarkFont {
    static let bundledName: String? = registerBundledFonts()

    private static func registerBundledFonts() -> String? {
        // Any .ttf/.otf anywhere in the app bundle, whatever the case of its extension.
        var urls: [URL] = []
        if let walker = FileManager.default.enumerator(at: Bundle.main.bundleURL, includingPropertiesForKeys: nil) {
            for case let url as URL in walker where ["ttf", "otf"].contains(url.pathExtension.lowercased()) {
                urls.append(url)
            }
        }
        urls.sort { $0.lastPathComponent < $1.lastPathComponent }
        var first: String? = nil
        for url in urls {
            _ = CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
            if first == nil,
               let descriptors = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL) as? [CTFontDescriptor],
               let descriptor = descriptors.first,
               let name = CTFontDescriptorCopyAttribute(descriptor, kCTFontNameAttribute) as? String {
                first = name
            }
        }
        return first
    }

    /// The font sized so its capitals are `capHeight` points tall, whatever its proportions.
    static func font(capHeight: CGFloat) -> UIFont {
        let probe = base(size: 100)
        let ratio = probe.capHeight / probe.pointSize
        return base(size: ratio > 0 ? capHeight / ratio : 44)
    }

    private static func base(size: CGFloat) -> UIFont {
        if let name = bundledName, let font = UIFont(name: name, size: size) { return font }
        let system = UIFont.systemFont(ofSize: size, weight: .bold)
        if let rounded = system.fontDescriptor.withDesign(.rounded) {
            return UIFont(descriptor: rounded, size: size)
        }
        return system
    }
}

extension Bundle {
    var shortVersion: String {
        infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
    }
}
