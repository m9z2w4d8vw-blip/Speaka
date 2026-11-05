import Foundation

/// The steps of a Start or a Stop after Bluetooth restarts, in the order the tweak goes through them inside
/// bluetoothd. Each one is read from a status the tweak publishes (Tweak.xm, BTPSV89Session.c), so when a Start fails,
/// the log shows the step that failed and the tweak's own reason. tests/test_speaka_app.py checks every key and prefix
/// here against the tweak's source.
///
/// Bluetooth arms the speaker and installs its hooks as it launches, so the first two steps finish together with the
/// restart. Bluetooth registers its own endpoints only once it is powered on, and the rest follow from that, a second
/// or two later.
struct SessionStep {
    enum State: Equatable {
        case waiting(String?)   // why it's still waiting, if the tweak has said
        case done
        case failed(String)     // the tweak's reason
    }

    let running: String   // while it runs: "Adding the speaker"
    let done: String      // once it's done: "Speaker added"
    let failed: String    // if it fails: "Speaker not added"
    let stalled: String   // what the header says if it's still waiting when time runs out
    let check: ([String: String]) -> State

    /// How long a request may take altogether, the same as Settings > Speaka (SPTweakClient).
    static let timeout: TimeInterval = 45

    static let start: [SessionStep] = [arm, hooks, endpoints, room, audioPath, speaker, advertising]
    static let stop: [SessionStep] = [switchOff]

    static let arm = SessionStep(
        running: "Arming the speaker", done: "Speaker armed", failed: "Speaker not armed",
        stalled: "Bluetooth restarted but didn't arm the speaker."
    ) { status in
        let trial = status["diag.v82.trial", default: ""]
        if trial.hasPrefix("ARMED") { return .done }
        if trial.hasPrefix("REFUSED") { return .failed(trial) }
        if SessionStep.session(status).hasSuffix("result=REFUSED") {
            return .failed(trial.hasPrefix("OFF") ? "the switches that arm it weren't set" : trial)
        }
        return .waiting(nil)
    }

    /// Every hook the speaker needs, by the status its installer leaves (all set before the restart is reported).
    static let hookStatuses: [(key: String, installed: [String], name: String)] = [
        ("diag.v80_config_lookup", ["ARMED", "OBSERVED", "NO_INDEX3"], "endpoint-count hook"),
        ("diag.v82.rx", ["installed"], "incoming-signaling hook"),
        ("diag.v82.tx", ["installed", "TX capture active"], "outgoing-signaling hook"),
        ("diag.v86.media", ["V87 ARMED"], "audio hook"),
        ("diag.v882.idle_guard", ["v8.8.2"], "idle guard"),
        ("diag.sdp_probe", ["installed"], "advertising hook"),
    ]

    /// The registration-loop hook's own failures. Its status moves on once it runs, so any other text means installed.
    static let loopHookFailures = ["OFF", "REFUSED: required", "REFUSED: registration-loop", "FAILED: registration-loop"]

    static let hooks = SessionStep(
        running: "Installing the hooks", done: "Hooks installed", failed: "A hook wasn't installed",
        stalled: "Bluetooth didn't report its hooks."
    ) { status in
        for hook in SessionStep.hookStatuses {
            let value = status[hook.key, default: ""]
            if !hook.installed.contains(where: value.hasPrefix) {
                return .failed("the \(hook.name): \(value.isEmpty ? "not reported" : value)")
            }
        }
        let loop = status["diag.v83.audio_table", default: ""]
        if loop.isEmpty || SessionStep.loopHookFailures.contains(where: loop.hasPrefix) {
            return .failed("the registration-loop hook: \(loop.isEmpty ? "not reported" : loop)")
        }
        return .done
    }

    static let endpoints = SessionStep(
        running: "Registering Bluetooth's endpoints", done: "Bluetooth's endpoints registered",
        failed: "Bluetooth's endpoints failed",
        stalled: "Bluetooth hasn't registered its endpoints. Check that Bluetooth is on."
    ) { status in
        let error = status["diag.native_source_error", default: ""]
        if !error.isEmpty { return .failed("one of Bluetooth's own endpoints failed, \(error)") }
        if let count = Int(status["diag.native_source_ok", default: ""]), count >= 6 { return .done }
        return .waiting(nil)
    }

    static let room = SessionStep(
        running: "Making room for the speaker", done: "Room made for the speaker", failed: "No room for the speaker",
        stalled: "Bluetooth didn't report room for the speaker."
    ) { status in
        let slots = status["diag.v81_seven_slots", default: ""]
        if slots.hasPrefix("ONE SHOT result: live_slots=7 ") { return .done }
        if slots.hasPrefix("ONE SHOT result:") || slots.hasPrefix("REFUSED") { return .failed(slots) }
        let lookup = status["diag.v80_config_lookup", default: ""]
        if lookup.hasPrefix("NO_INDEX3") { return .failed(lookup) }
        return .waiting(nil)
    }

    /// Once its one attempt is used up, everything that stops the speaker being added lands in the audio table's
    /// status; WAITING means a prerequisite was missing and Bluetooth's next power-on tries again.
    static let tableFailures = ["REFUSED", "ROLLED BACK", "UNSAFE", "FAILED"]

    static let audioPath = SessionStep(
        running: "Preparing the audio path", done: "Audio path ready", failed: "Audio path not ready",
        stalled: "Bluetooth didn't prepare the audio path."
    ) { status in
        let audio = status["diag.audio_receive", default: ""]
        if audio.hasPrefix("v8.8 callback ready") { return .done }
        if audio.hasPrefix("REFUSED") { return .failed(audio) }
        let table = status["diag.v83.audio_table", default: ""]
        if SessionStep.tableFailures.contains(where: table.hasPrefix) { return .failed(table) }
        return .waiting(table.hasPrefix("WAITING") ? table : nil)
    }

    static let speaker = SessionStep(
        running: "Adding the speaker", done: "Speaker added", failed: "Speaker not added",
        stalled: "Bluetooth didn't add the speaker."
    ) { status in
        let sep = status["diag.v82.sep", default: ""]
        if sep.hasPrefix("LOCAL register=0 SEID=7") { return .done }
        if sep.hasPrefix("LOST") { return .failed(sep) }
        let table = status["diag.v83.audio_table", default: ""]
        if SessionStep.tableFailures.contains(where: table.hasPrefix) { return .failed(table) }
        return .waiting(table.hasPrefix("WAITING") ? table : nil)
    }

    static let advertising = SessionStep(
        running: "Advertising the speaker", done: "Speaker advertised", failed: "Speaker not advertised",
        stalled: "Bluetooth didn't publish the speaker's record."
    ) { status in
        // "%#llx" prints a zero result as plain "0".
        let sdp = status["diag.v82.sdp", default: ""]
        if sdp.hasPrefix("LOCAL add=0 ") { return .done }
        if sdp.hasPrefix("LOCAL add=") { return .failed(sdp) }
        let table = status["diag.v83.audio_table", default: ""]
        if table.hasPrefix("SDP publish failed") { return .failed(table) }
        return .waiting(nil)
    }

    static let switchOff = SessionStep(
        running: "Switching the speaker off", done: "Speaker switched off", failed: "Speaker still on",
        stalled: "Bluetooth restarted but didn't report the speaker off."
    ) { status in
        let result = SessionStep.session(status)
        if result.hasSuffix("result=OFF") { return .done }
        if result.hasSuffix("result=STILL_ARMED") { return .failed("Bluetooth restarted with the speaker still armed") }
        return .waiting(nil)
    }

    /// "start nonce=… pid=… result=ARMED", the line that tells the app Bluetooth restarted for its request.
    private static func session(_ status: [String: String]) -> String {
        status["diag.v89.session", default: ""]
    }
}
