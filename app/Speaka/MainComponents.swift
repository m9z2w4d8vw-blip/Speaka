//
//  MainComponents.swift
//  Speaka
//
//  The main screen's pieces, the same as Caz's (Caz/MainComponents.swift): header, action menu, the big button
//  and the log it expands into. Each mirrors one of Dopamine's DO classes: DOHeaderView, DOActionMenuView and
//  DOActionMenuButton, DOJailbreakButton, DOLyricsLogView and DOLyricsLogItemView, DOLoadingIndicator and
//  DODoubleHelixIndicator.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import CoreText
import SwiftUI
import UIKit

struct HeaderView: View {
    let subtitle: String
    let detail: String
    var detailIsWarning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Wordmark()
            Text(subtitle)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.white)
            Text(detail)
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(detailIsWarning ? Theme.failure : Theme.secondary)
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 5)
    }
}

/// "Speaka" placed like Caz's title and Dopamine's logo: a 40 pt slot at the top of the header, drawn 7 pt
/// higher (DOHeaderView's alignment inset), with capitals 32 pt tall like the Dopamine wordmark.
struct Wordmark: View {
    var body: some View {
        if let image = UIImage(named: "Wordmark") {
            // Dopamine draws its title from an image (Dopamine.imageset): 40 pt tall, 7 pt high. Speaka's
            // Wordmark.imageset is drawn the same way as Caz's, in Fonarto Regular (app/scripts/make_wordmark.py).
            Image(uiImage: image)
                .resizable()
                .scaledToFit()
                .frame(height: 40, alignment: .leading)
                .offset(y: -7)
                .accessibilityLabel("Speaka")
                .accessibilityAddTraits(.isHeader)
        } else {
            text
        }
    }

    private var text: some View {
        let font = WordmarkFont.font(capHeight: 32)
        return Text("Speaka")
            .font(Font(font as CTFont))
            .foregroundStyle(.white)
            .fixedSize()
            .alignmentGuide(.top) { d in d[.firstTextBaseline] - font.capHeight }
            .frame(height: 40, alignment: .topLeading)
            .offset(y: -7)
            .accessibilityAddTraits(.isHeader)
    }
}

struct MenuItem: Identifiable {
    let id: String
    let title: String
    let symbol: String
    var chevron = false
    var enabled = true
    /// v9.8: a row that acts only once held this long (Uninstall). A tap alone does nothing.
    var holdSeconds: Double? = nil
    /// Its action is running: a spinner in the icon's place, and the row greyed out like a disabled one.
    var busy = false
    let action: () -> Void
}

struct ActionMenu: View {
    let items: [MenuItem]
    let rowHeight: CGFloat
    let innerPadding: CGFloat

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                Group {
                    if item.holdSeconds != nil {
                        HoldMenuRow(item: item, height: rowHeight)
                    } else {
                        MenuRow(item: item, height: rowHeight)
                    }
                }
                .overlay(alignment: .bottom) {
                    if index < items.count - 1 {
                        Capsule()
                            .fill(Theme.separator)
                            .frame(height: 1)
                            .padding(.horizontal, 10)
                    }
                }
            }
        }
        .padding(.horizontal, innerPadding)
        .padding(.vertical, Metrics.innerTopPadding)
        .background(Theme.menu, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

struct MenuRow: View {
    let item: MenuItem
    let height: CGFloat

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            item.action()
        } label: {
            // DOActionMenuButton: a plain UIButton configuration (12 pt content insets, 10 pt image
            // padding, 17 pt title, symbols drawn at about 19 pt), with the chevron pinned 10 pt from the
            // button's trailing edge.
            HStack(spacing: 10) {
                Image(systemName: item.symbol)
                    .font(.system(size: 19, weight: .medium))
                Text(item.title)
                    .font(.system(size: 17))
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Spacer(minLength: 0)
            }
            .foregroundStyle(Theme.text)
            .padding(.horizontal, 12)
            .frame(height: height)
            .overlay(alignment: .trailing) {
                if item.chevron {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 16, weight: .regular))
                        .foregroundStyle(Color.white.opacity(0.6))
                        .padding(.trailing, 10)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(MenuButtonStyle())
        .disabled(!item.enabled)
        .opacity(item.enabled ? 1 : 0.45)
    }
}

/// v9.8, Uninstall: laid out exactly like a MenuRow, but a tap does nothing. Held down, a gray fill sweeps across the
/// row from left to right over the hold, and everything it has covered dims to Caz's disabled 45%, so the part it has
/// passed looks like a disabled row while the rest is still white. The title stays Uninstall throughout; let go early
/// and the edge runs back. Held the whole time: a warning haptic and the action, with no second prompt, and the whole
/// row keeps the disabled look while it runs, with a spinner in the icon's place and "Uninstalling...". It doesn't dim
/// while pressed, as other rows do, so the sweep is the only thing a press does.
struct HoldMenuRow: View {
    let item: MenuItem
    let height: CGFloat
    /// How far the hold has got, 0 to 1. Animated: linearly up to 1 over the hold, quickly back to 0 on letting go.
    @State private var progress: CGFloat = 0

    /// Caz's disabled row: what the covered part of the row looks like.
    static let dimmed = 0.45
    /// The gray behind the covered part.
    static let cover = Color.white.opacity(0.10)

    private var seconds: Double { item.holdSeconds ?? 4 }

    /// Once the action runs, the whole row stays covered.
    private var shown: CGFloat { item.busy ? 1 : progress }

    private var content: some View {
        HStack(spacing: 10) {
            ZStack {
                if item.busy {
                    Spinner()
                } else {
                    Image(systemName: item.symbol)
                        .font(.system(size: 19, weight: .medium))
                }
            }
            Text(item.busy ? "Uninstalling..." : item.title)
                .font(.system(size: 17))
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Spacer(minLength: 0)
        }
        .foregroundStyle(Theme.text)
        .padding(.horizontal, 12)
        .frame(height: height)
    }

    var body: some View {
        // The fill, then the row twice: white where the edge hasn't reached, dimmed where it has. One animatable shape
        // draws the fill and cuts both copies, so all three move together.
        ZStack {
            HoldPortion(progress: shown).fill(Self.cover)
            content.mask { HoldPortion(progress: shown, leading: false) }
            content.opacity(Self.dimmed).mask { HoldPortion(progress: shown) }
        }
        .frame(height: height)
        .contentShape(Rectangle())
        .opacity(item.enabled || item.busy ? 1 : Self.dimmed)
        .onLongPressGesture(minimumDuration: seconds, maximumDistance: 40) {
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            item.action()
        } onPressingChanged: { pressing in
            if pressing { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
            withAnimation(pressing ? Animation.linear(duration: seconds) : Animation.easeOut(duration: 0.25)) {
                progress = pressing ? 1 : 0
            }
        }
        .disabled(!item.enabled || item.busy)
        // The content is drawn twice; VoiceOver hears it once.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(item.busy ? "Uninstalling" : item.title)
        .accessibilityAddTraits(.isButton)
        .accessibilityHint("Hold for \(Int(seconds)) seconds to uninstall Speaka and its tweak.")
    }
}

/// The part of a frame a hold has covered (from the leading edge to `progress` of the width), or the part it hasn't.
/// A Shape fills whatever frame it's given, so the row needn't measure itself, and `animatableData` makes the edge
/// sweep smoothly instead of jumping to where the hold is going.
struct HoldPortion: Shape {
    var progress: CGFloat
    var leading = true

    var animatableData: CGFloat {
        get { progress }
        set { progress = newValue }
    }

    func path(in rect: CGRect) -> Path {
        let edge = rect.width * min(max(progress, 0), 1)
        return Path(leading ? CGRect(x: rect.minX, y: rect.minY, width: edge, height: rect.height)
                            : CGRect(x: rect.minX + edge, y: rect.minY, width: rect.width - edge, height: rect.height))
    }
}

/// The big button under the menu (DOJailbreakButton before it expands).
struct ActionButton: View {
    let title: String
    let symbol: String
    let enabled: Bool
    let action: () -> Void

    var body: some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            action()
        } label: {
            HStack(spacing: 10) {
                Image(systemName: symbol)
                    .font(.system(size: 19, weight: .medium))
                Text(title)
                    .font(.system(size: 17))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            .foregroundStyle(Theme.text)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(MenuButtonStyle())
        .disabled(!enabled)
        .background(Theme.menu, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .opacity(enabled ? 1 : 0.7)
    }
}

/// Who is connected and where the audio plays, in the slot where Caz shows Update Available: the same 19 pt
/// symbol and 17 pt title 10 pt apart with 12 pt side insets, as a label rather than a button.
struct StatusLineView: View {
    let line: SpeakaModel.StatusLine

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: line.symbol)
                .font(.system(size: 19, weight: .medium))
            Text(line.text)
                .font(.system(size: 17))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .foregroundStyle(Theme.text)
        .padding(.horizontal, 12)
        .frame(maxHeight: .infinity)
        .accessibilityElement(children: .combine)
    }
}

/// The big button after it expands: a panel reaching past the bottom of the screen,
/// with a title and the lyrics-style log.
struct LogSheet: View {
    @ObservedObject var model: SpeakaModel
    let visibleHeight: CGFloat

    var body: some View {
        ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Theme.menu)
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    DoubleHelix()
                        .frame(width: 30, height: 12)
                    Text(model.sheetTitle)
                        .font(.system(size: 18, weight: .regular))
                        .foregroundStyle(.white)
                }
                .padding(.top, 20)
                LyricsLog(lines: model.lyrics, texts: model.logTexts,
                          bottomInset: Metrics.isHomeButtonDevice ? 50 : 80)
            }
            .frame(height: visibleHeight, alignment: .top)
        }
    }
}

/// New lines appear at the bottom and push older ones up, where they fade out.
struct LyricsLog: View {
    let lines: [LyricLine]
    /// Every line this log could show (SpeakaModel.possibleLines). The column is as wide as the widest of them from the
    /// first line on, so no line moves sideways when a longer one appears or shrinks: it stays centered, as in Caz, but
    /// never re-centers.
    let texts: [LyricText]
    let bottomInset: CGFloat

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            // Invisible and one row tall: only its width counts.
            ZStack(alignment: .leading) {
                ForEach(texts, id: \.self) { text in
                    LyricRow.measure(text)
                }
            }
            .hidden()
            VStack(alignment: .leading, spacing: 0) {
                Spacer(minLength: 0)
                ForEach(lines.suffix(14)) { line in
                    LyricRow(line: line)
                        .frame(height: 40)
                        .transition(.asymmetric(
                            insertion: .opacity.combined(with: .offset(y: 8)),
                            removal: .opacity
                        ))
                }
            }
        }
        .padding(.horizontal, 25)
        .padding(.bottom, max(0, bottomInset - 40))
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        .clipped()
        .mask {
            LinearGradient(stops: [
                .init(color: .clear, location: 0),
                .init(color: .black, location: 0.45),
                .init(color: .black, location: 1),
            ], startPoint: .top, endPoint: .bottom)
        }
        .animation(.easeOut(duration: 0.3), value: lines)
    }
}

struct LyricRow: View {
    let line: LyricLine

    var body: some View {
        HStack(spacing: 15) {
            ZStack {
                switch line.status {
                case .running:
                    Spinner()
                case .completed:
                    Image(systemName: "checkmark")
                        .font(.system(size: 19))
                case .failed:
                    Image(systemName: "exclamationmark.circle")
                        .font(.system(size: 19))
                case .success:
                    Image(systemName: line.symbol)
                        .font(.system(size: 19))
                }
            }
            .frame(width: 30, height: 30)
            Text(line.text)
                .font(.system(size: line.status == .running ? 20 : 18))
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
        .foregroundStyle(.white)
        .opacity(line.status == .running ? 0.9 : 0.5)
        .animation(.easeOut(duration: 0.2), value: line.status)
    }
}

extension LyricRow {
    /// A row's size with `text` in it, for the log's column: the same 30 pt icon slot, 15 pt gap and type as the row
    /// itself (20 pt while running, 18 pt after), without the icon.
    static func measure(_ text: LyricText) -> some View {
        HStack(spacing: 15) {
            Color.clear
                .frame(width: 30, height: 30)
            Text(text.text)
                .font(.system(size: text.running ? 20 : 18))
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
    }
}

/// A ring that turns once a second.
struct Spinner: View {
    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            Circle()
                .trim(from: 0, to: 0.72)
                .stroke(Color.white, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                .frame(width: 20, height: 20)
                .rotationEffect(.degrees(t.truncatingRemainder(dividingBy: 1.0) * 360))
        }
    }
}

/// Two rows of ten dots that wave past each other, every 2.3 seconds.
struct DoubleHelix: View {
    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            ZStack(alignment: .topLeading) {
                ForEach(0..<10, id: \.self) { i in
                    let p = DoubleHelix.wave(t - 0.1 * Double(i))
                    Circle()
                        .fill(Color.white.opacity(0.8))
                        .frame(width: 2, height: 2)
                        .offset(x: CGFloat(i) * 3, y: 8 - 8 * p)
                    Circle()
                        .fill(Color.white)
                        .frame(width: 2, height: 2)
                        .offset(x: CGFloat(i) * 3, y: 8 * p)
                }
            }
            .frame(width: 30, height: 12, alignment: .topLeading)
        }
    }

    static func wave(_ t: Double) -> CGFloat {
        let period = 2.3
        var phase = t.truncatingRemainder(dividingBy: period)
        if phase < 0 { phase += period }
        return CGFloat(0.5 - 0.5 * cos(phase / period * 2 * Double.pi))
    }
}
