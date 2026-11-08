//
//  ConnectModel.swift
//  Speaka
//
//  The Connect to page (v9.9.12): its rows, refreshed every second while it's open, one bounded scan when it opens, and
//  asking iOS to connect a paired computer. The devices come from SPBluetoothDevices, merged with Speaka's history and
//  its live stream by shared/DeviceRoster.h.
//

import Combine
import Foundation

final class ConnectModel: ObservableObject {
    struct Row: Identifiable, Equatable {
        let id: String              // the Bluetooth address: one row per device, whatever its name
        let name: String
        let kind: SPDeviceKind
        let state: SPDeviceState
        let section: SPDeviceSection
        let lastConnected: Date?
        let paired: Bool
        let canConnect: Bool
        let failure: String?

        init(_ row: SPDeviceRow) {
            id = row.address
            name = row.name
            kind = row.kind
            state = row.state
            section = row.section
            lastConnected = row.lastConnected
            paired = row.paired
            canConnect = row.canConnect
            failure = row.failure
        }
    }

    @Published private(set) var rows: [Row] = []
    @Published private(set) var availability: SPBluetoothAvailability = .starting
    @Published private(set) var detail = ""
    @Published private(set) var scanning = false
    @Published private(set) var canScan = false
    /// What the last tap needs said: "pair it from the computer first", ...
    @Published private(set) var notice: String? = nil
    /// Speaka was playing when the page opened, so it didn't scan by itself: a scan takes radio time from the stream.
    @Published private(set) var heldForAudio = false

    static let scanSeconds: TimeInterval = 12
    private let devices = SPBluetoothDevices.shared()
    private var timer: Timer?
    private var streamAddress = ""
    private var streaming = false
    private var playing = false
    private var scannedOnOpen = false

    func rows(in section: SPDeviceSection) -> [Row] { rows.filter { $0.section == section } }

    /// The page opened (or Speaka came back to the front with it open).
    func open(streamAddress: String, streaming: Bool, playing: Bool) {
        self.streamAddress = streamAddress
        self.streaming = streaming
        self.playing = playing
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
    }

    /// Speaka's stream changed (a computer connected, or stopped): Connected follows it.
    func update(streamAddress: String, streaming: Bool, playing: Bool) {
        self.streamAddress = streamAddress
        self.streaming = streaming
        self.playing = playing
        refresh()
    }

    /// The page closed, or Speaka left the front: no scanning in the background, no polling.
    func close() {
        timer?.invalidate()
        timer = nil
        devices.stopScanning()
        scanning = false
        scannedOnOpen = false
    }

    func rescan() {
        notice = nil
        heldForAudio = false            // asked for: the note about not scanning no longer applies
        devices.startScanning(for: Self.scanSeconds)
        refresh()
    }

    func connect(_ row: Row) {
        let outcome = devices.connect(address: row.id)
        switch outcome {
        case .requested:
            notice = nil
        case .alreadyConnected:
            notice = "\(row.name) is already connected to this iPhone. To play audio, choose Speaka as the output on \(row.name)."
        case .notPaired:
            notice = "Pair \(row.name) from the computer first: Bluetooth & devices > Add device > Bluetooth, then this iPhone."
        case .unsupported:
            notice = "This iOS can't be asked to connect it. Connect from \(row.name) instead."
        case .notFound:
            notice = "\(row.name) isn't paired with this iPhone any more. Pair it again from the computer."
        case .bluetoothNotReady:
            notice = devices.availabilityDetail.isEmpty ? "Bluetooth isn't ready." : devices.availabilityDetail
        @unknown default:
            notice = nil
        }
        refresh()
    }

    func refresh() {
        let next = devices.rows(streamAddress: streamAddress, streaming: streaming).map(Row.init)
        if next != rows { rows = next }
        if availability != devices.availability { availability = devices.availability }
        if detail != devices.availabilityDetail { detail = devices.availabilityDetail }
        if canScan != devices.canScan { canScan = devices.canScan }
        if scanning != devices.scanning { scanning = devices.scanning }
        // One scan when the page opens, once Bluetooth answers - unless Speaka is playing.
        if !scannedOnOpen && availability == .ready && canScan {
            scannedOnOpen = true
            if playing {
                heldForAudio = true
            } else {
                heldForAudio = false
                devices.startScanning(for: Self.scanSeconds)
                scanning = devices.scanning
            }
        }
    }
}
