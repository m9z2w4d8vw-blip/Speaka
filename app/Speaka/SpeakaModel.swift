//
//  SpeakaModel.swift
//  Speaka
//
//  Drives the screen. It reads the tweak through SPTweakClient, which keeps the same rules as Settings > Speaka, and
//  turns a Start or a Stop into the log the big button opens into, the way Caz's CleanerModel does for a scan.
//  The log's lines follow Caz's, which follow Dopamine's DOLyricsLogItemView.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import Combine
import Foundation
import UIKit

/// One line of the log the big button opens into (Caz's LyricLine, Dopamine's DOLyricsLogItemView).
struct LyricLine: Identifiable, Equatable {
    enum Status: Equatable { case running, completed, failed, success }
    let id = UUID()
    var text: String
    var status: Status
    /// The icon of the final line, which says how it ended.
    var symbol: String = "checkmark.circle"
}

/// A line the log could show, for sizing its column: its text, and whether it would be the running line, which is
/// drawn larger.
struct LyricText: Hashable {
    let text: String
    let running: Bool
}

/// One of the three places the audio can play, from SPTweakClient: the same list as Settings > Speaka.
struct OutputMode: Identifiable, Equatable {
    let key: String
    let title: String
    let symbol: String
    let detail: String
    var id: String { key }
}

final class SpeakaModel: ObservableObject {
    /// What the tweak and the audio helper last reported (SPStatus), as plain values.
    struct Snapshot: Equatable {
        var session: SPSession = .noTweak
        var build = ""
        var outputMode = "speaker"
        var receiving = false
        var playing = false
        var playingOn = ""
        var waitingReason = ""
        var volumeZero = false
        var detail = ""
    }

    /// The line above the big button, where Caz shows Update Available.
    struct StatusLine: Equatable {
        let symbol: String
        let text: String
    }

    static let outputModes: [OutputMode] = SPTweakClient.outputModes().map { mode in
        OutputMode(key: mode["key"] ?? "", title: mode["title"] ?? "",
                   symbol: mode["symbol"] ?? "speaker.wave.2", detail: mode["detail"] ?? "")
    }

    @Published private(set) var snapshot = Snapshot()
    @Published private(set) var request: SPRequest? = nil
    @Published private(set) var lyrics: [LyricLine] = []
    /// Every line a Start's or a Stop's log could show. Both logs size their column from it, so a line sits in the same
    /// place in either, and nothing moves sideways while one runs.
    let logTexts = SpeakaModel.possibleLines()
    @Published private(set) var sheetVisible = false
    @Published private(set) var sheetTitle = "Starting"
    @Published private(set) var completedSteps = 0
    @Published private(set) var successCount = 0
    @Published private(set) var problem: String? = nil
    /// A computer iOS reports as connected, and the one computer paired with this iPhone (SPConnectedDevice).
    @Published private(set) var computerName: String? = nil
    @Published private(set) var pairedName: String? = nil

    private let client = SPTweakClient()
    /// The tweak's own status block, which the steps of a Start or a Stop are read from.
    private var status: [String: String] = [:]
    /// The steps still to come after Bluetooth restarts (SessionSteps.swift), and the one running now.
    private var steps: [SessionStep] = []
    private var stepIndex = 0
    private var timer: Timer? = nil
    private var active = false
    private var problemSession: SPSession? = nil
    private var computerNameDate: Date? = nil
    private var outputChangeDate: Date? = nil

    init() {
        refresh()
    }

    // MARK: - What the screen shows

    var isBusy: Bool { request != nil || sheetVisible }

    /// The big button stops a speaker that is on, still starting, or stuck half-started (the restart that Stop does
    /// clears that), and starts it otherwise.
    var buttonStops: Bool {
        switch snapshot.session {
        case .on, .starting, .failed:
            return true
        default:
            return false
        }
    }

    var buttonTitle: String { buttonStops ? "Stop" : "Start" }

    var buttonSymbol: String { buttonStops ? "stop" : "play" }

    /// Greyed out while nothing would answer it (no tweak, or an older one still running in Bluetooth) and while a
    /// request is in flight, like Caz's Clean button before a scan.
    var canPress: Bool {
        guard request == nil else { return false }
        switch snapshot.session {
        case .noTweak, .oldTweak:
            return false
        default:
            return true
        }
    }

    /// First line under the name, as in Caz.
    var headerSubtitle: String { "iOS \(UIDevice.current.systemVersion)" }

    /// Second line under the name: "by Parv", or whatever needs attention, in the warning colour.
    var headerDetail: String { headerWarning ?? "by Parv" }

    var headerWarning: String? {
        if let problem {
            return problem
        }
        switch snapshot.session {
        case .noTweak:
            return "The Speaka tweak isn't running. Install its package, then open Speaka again."
        case .oldTweak:
            return "Bluetooth still runs the old Speaka tweak. Reinstall the package to restart it."
        case .lost:
            return "Bluetooth was turned off and on, which removed the speaker. Tap Start."
        case .failed:
            return "The speaker didn't start: \(snapshot.detail)"
        case .refused:
            return "Bluetooth refused the speaker: \(snapshot.detail)"
        default:
            break
        }
        if snapshot.session == .on && snapshot.playing && snapshot.volumeZero {
            return "The volume is at 0%. Turn it up while audio plays."
        }
        return nil
    }

    /// Who is connected and where it plays. Hidden while the speaker is off and while a request is in flight.
    var statusLine: StatusLine? {
        guard request == nil else { return nil }
        switch snapshot.session {
        case .on:
            guard snapshot.receiving else {
                // Connected but not playing yet: name the PC as soon as iOS lists it as connected.
                if let name = computerName {
                    return StatusLine(symbol: "laptopcomputer", text: "\(name) · connected")
                }
                return StatusLine(symbol: "dot.radiowaves.left.and.right", text: "Connect from your PC")
            }
            // A PC is streaming, so the one paired computer is it, even if iOS doesn't list it as connected.
            let who = computerName ?? pairedName ?? "Your PC"
            if snapshot.playing {
                let place = snapshot.playingOn.isEmpty ? "this iPhone" : snapshot.playingOn
                return StatusLine(symbol: "laptopcomputer", text: "\(who) · \(place)")
            }
            // Why it is silent matters more than who is sending, and both rarely fit on one line.
            let reason = snapshot.waitingReason.isEmpty ? "no output yet" : snapshot.waitingReason
            return StatusLine(symbol: "laptopcomputer", text: "Waiting: \(reason)")
        case .starting:
            return StatusLine(symbol: "hourglass", text: "Adding the speaker")
        default:
            return nil
        }
    }

    // MARK: - Polling, every 0.5 s like Settings > Speaka

    /// Polling pauses while the app isn't active, except to follow a Start or Stop that is in flight.
    func setActive(_ isActive: Bool) {
        active = isActive
        if isActive {
            refresh()
            startPolling()
        } else if request == nil {
            stopPolling()
        }
    }

    private func startPolling() {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        refresh()   // first, so the steps read this poll's status
        guard let pending = request else { return }
        if pending.relaunched {
            advance(pending)
            return
        }
        // Until Bluetooth restarts, SPTweakClient follows the request, as for Settings > Speaka.
        let state = client.pollRequest(pending)
        if pending.relaunched {
            relaunched(pending)
        } else if state == .finished {
            finish(pending)
        }
    }

    func refresh() {
        let status = client.readStatus()
        self.status = status.diagnostics.compactMapValues { $0 as? String }
        var next = Snapshot()
        next.session = status.session
        next.build = status.build
        next.outputMode = status.outputMode
        next.receiving = status.receiving
        next.playing = status.playing
        next.playingOn = status.playingOn
        next.waitingReason = status.waitingReason
        next.volumeZero = status.volumeZero
        next.detail = status.detail
        // Right after a change the preference can still read the old value for a moment: don't flip back.
        if let changed = outputChangeDate, Date().timeIntervalSince(changed) < 2.0 {
            next.outputMode = snapshot.outputMode
        }
        if next != snapshot {
            snapshot = next
        }
        // A problem stays in the header until the speaker's state moves on.
        if let problemSession, problemSession != next.session {
            problem = nil
            self.problemSession = nil
        }
        // The PC's name is looked up at most every 3 s while the speaker is on, playing or not, so it shows as soon as
        // the PC connects; nil means "Connect from your PC" before audio, and "Your PC" during it.
        if next.session == .on {
            let stale = computerNameDate.map { Date().timeIntervalSince($0) > 3.0 } ?? true
            if stale {
                computerNameDate = Date()
                let connected = SPConnectedDevice.connectedComputerName()
                if connected != computerName {
                    computerName = connected
                }
                let paired = SPConnectedDevice.pairedComputerName()
                if paired != pairedName {
                    pairedName = paired
                }
            }
        } else if computerName != nil || pairedName != nil {
            computerName = nil
            pairedName = nil
            computerNameDate = nil
        }
    }

    // MARK: - The big button

    /// Start or Stop. The button opens into the log, the tweak restarts Bluetooth, and each step gets a line.
    func toggle() {
        guard canPress else { return }
        let action = buttonStops ? "stop" : "start"
        problem = nil
        problemSession = nil
        lyrics.removeAll()
        sheetTitle = action == "start" ? "Starting" : "Stopping"
        sheetVisible = true
        startLyric("Restarting Bluetooth")
        var error: NSString? = nil
        guard let pending = client.sendAction(action, error: &error) else {
            let reason = error.map { $0 as String } ?? "The request could not be sent."
            fail("Couldn't reach Bluetooth", problem: reason)
            collapseSoon(after: Self.failedLogSeconds)
            return
        }
        request = pending
        startPolling()
    }

    private func relaunched(_ pending: SPRequest) {
        finishLyric("Bluetooth restarted")
        steps = pending.action == "start" ? SessionStep.start : SessionStep.stop
        stepIndex = 0
        startLyric(steps[0].running)
    }

    /// One step per poll, so the log reads one line at a time even when Bluetooth reports several at once. The step
    /// running now finishes, fails with the tweak's reason, or keeps waiting until the request runs out of time.
    private func advance(_ pending: SPRequest) {
        guard stepIndex < steps.count else { return }
        let step = steps[stepIndex]
        switch step.check(status) {
        case .done:
            finishLyric(step.done)
            stepIndex += 1
            if stepIndex < steps.count {
                startLyric(steps[stepIndex].running)
            } else {
                complete(pending)
            }
        case .failed(let reason):
            stop(line: step.failed, problem: "\(step.failed): \(reason)")
        case .waiting(let reason):
            if Date().timeIntervalSince(pending.started) > SessionStep.timeout {
                stop(line: step.failed, problem: reason.map { "\(step.failed): \($0)" } ?? step.stalled)
            }
        }
    }

    private func complete(_ pending: SPRequest) {
        end()
        if pending.action == "start" {
            finishLyrics(success: "Ready for your PC", symbol: "hifispeaker")
        } else {
            finishLyrics(success: "Bluetooth back to normal", symbol: "checkmark.circle")
        }
        collapseSoon()
    }

    /// The step that was running fails, and its reason stays in the header after the log folds away.
    private func stop(line: String, problem: String) {
        end()
        fail(line, problem: problem)
        collapseSoon(after: Self.failedLogSeconds)
    }

    /// A request that ends before Bluetooth restarts: SPTweakClient's verdict.
    private func finish(_ pending: SPRequest) {
        end()
        let failure = Self.failure(for: pending)
        fail(failure.line, problem: failure.problem)
        collapseSoon(after: Self.failedLogSeconds)
    }

    private func end() {
        request = nil
        steps = []
        stepIndex = 0
        if !active {
            stopPolling()
        }
    }

    /// A failed log stays up long enough to read which step failed (a finished one folds after Caz's 1.6 s).
    static let failedLogSeconds: TimeInterval = 6

    /// What the line says when a request ends before Bluetooth restarts, and what the header says afterwards. The
    /// titles are SPTweakClient's, which Settings > Speaka shares.
    static func failure(for pending: SPRequest) -> (line: String, problem: String) {
        if pending.resultTitle == "Still waiting" {
            return ("Bluetooth didn't restart",
                    "Bluetooth didn't restart. If devices don't reconnect, run killall -9 bluetoothd.")
        }
        return ("Bluetooth wasn't restarted", "Bluetooth wasn't restarted: \(reason(in: pending.resultMessage ?? ""))")
    }

    /// Every line a Start's or a Stop's log can show, each once, so the log can size its column to the widest of them
    /// before its first line (LyricsLog). Start's and Stop's together, so both logs get the same column and a line
    /// sits in the same place in either. tests/test_speaka_app.py checks that every line this model writes is here.
    static func possibleLines() -> [LyricText] {
        var lines = [LyricText(text: "Restarting Bluetooth", running: true)]
        for text in ["Bluetooth restarted", "Bluetooth didn't restart", "Bluetooth wasn't restarted",
                     "Couldn't reach Bluetooth", "Ready for your PC", "Bluetooth back to normal"] {
            lines.append(LyricText(text: text, running: false))
        }
        for step in SessionStep.start + SessionStep.stop {
            lines.append(LyricText(text: step.running, running: true))
            lines.append(LyricText(text: step.done, running: false))
            lines.append(LyricText(text: step.failed, running: false))
        }
        var seen = Set<LyricText>()
        return lines.filter { seen.insert($0).inserted }
    }

    /// "REFUSED nonce=… reason=stale request; Bluetooth not restarted" becomes "stale request".
    static func reason(in message: String) -> String {
        guard let range = message.range(of: "reason=") else { return message }
        return String(message[range.upperBound...].prefix(while: { $0 != ";" }))
    }

    // MARK: - Audio output

    func setOutputMode(_ mode: String) {
        guard mode != snapshot.outputMode else { return }
        outputChangeDate = Date()
        snapshot.outputMode = mode   // the checkmark moves at once
        if !client.setOutputMode(mode) {
            outputChangeDate = nil
            problem = "Couldn't save the audio output. Try again, or use Settings > Speaka."
            problemSession = snapshot.session
            refresh()
        }
    }

    // MARK: - The log (Caz's lyrics log)

    private func startLyric(_ text: String) {
        completeRunningLyric()
        lyrics.append(LyricLine(text: text, status: .running))
    }

    private func finishLyric(_ text: String) {
        if let index = lyrics.lastIndex(where: { $0.status == .running }) {
            lyrics[index].text = text
            lyrics[index].status = .completed
            completedSteps += 1
        } else {
            lyrics.append(LyricLine(text: text, status: .completed))
        }
    }

    private func completeRunningLyric() {
        for index in lyrics.indices where lyrics[index].status == .running {
            lyrics[index].status = .completed
            completedSteps += 1
        }
    }

    private func finishLyrics(success text: String, symbol: String) {
        completeRunningLyric()
        lyrics.append(LyricLine(text: text, status: .success, symbol: symbol))
        sheetTitle = "Done"
        successCount += 1
    }

    /// The step that was running fails with `text`, and the title becomes "Stopped", as in Caz.
    private func fail(_ text: String, problem: String?) {
        if let index = lyrics.lastIndex(where: { $0.status == .running }) {
            lyrics[index].text = text
            lyrics[index].status = .failed
        } else {
            lyrics.append(LyricLine(text: text, status: .failed))
        }
        sheetTitle = "Stopped"
        self.problem = problem
        problemSession = problem == nil ? nil : snapshot.session
    }

    /// Keep the finished log on screen for a moment, then fold the button back (1.6 s, as in Caz).
    private func collapseSoon(after seconds: TimeInterval = 1.6) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            self?.sheetVisible = false
        }
    }
}
