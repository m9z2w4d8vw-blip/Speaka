//
//  SpeakaUpdater.swift
//  Speaka
//
//  Checks GitHub for a newer Speaka and installs it: the tweak's .deb with dpkg (Bluetooth restarts, and the iPhone
//  resprings at the end) and the app's .tipa with TrollStore. This is Caz's UpdateModel (Caz/UpdateModel.swift),
//  which follows Dopamine's updater (DOUIManager, DOUpdateViewController, DODownloadViewController), with the .deb added.
//  Dopamine: Copyright (c) 2023-2024 Lars Fröder (opa334), MIT License (see LICENSE-Dopamine.txt).
//

import Combine
import Foundation
import UIKit

/// One GitHub release of Speaka. The app and the tweak in it carry the same version number.
struct SpeakaRelease: Identifiable, Equatable {
    let id: Int
    let name: String
    let body: String
    let version: [Int]
    let tipaURL: URL?
    let debURL: URL?
}

final class SpeakaUpdater: ObservableObject {
    enum Stage: Equatable {
        case changelog
        case downloading
        case installing
        case installed
        case failed(String)
    }

    static let apiURL = URL(string: "https://api.github.com/repos/m9z2w4d8vw-blip/Speaka/releases")!
    static let releasesPage = URL(string: "https://github.com/m9z2w4d8vw-blip/Speaka/releases")!
    static let packageID = "com.parv.speaka"

    /// Releases newer than what's installed, newest first: the changelog.
    @Published private(set) var updates: [SpeakaRelease] = []
    /// The newest release whose .tipa is newer than this app, if any.
    @Published private(set) var tipa: SpeakaRelease? = nil
    /// The newest release whose .deb is newer than the installed tweak, if any.
    @Published private(set) var deb: SpeakaRelease? = nil
    @Published var stage: Stage = .changelog
    /// What the circle shows. It eases toward the real progress like Caz's (25% of the gap per frame).
    @Published private(set) var progress: Double = 0

    private struct Part {
        let url: URL
        let name: String
    }

    private var target: Double = 0
    private var timer: Timer?
    private var task: URLSessionDownloadTask?
    private var partCount = 1
    private var partsDone = 0
    private var checking = false
    private var checked = false

    var isAvailable: Bool { tipa != nil || deb != nil }
    var isBusy: Bool { stage == .downloading || stage == .installing }
    /// A new tweak restarts Bluetooth and ends with a respring.
    var resprings: Bool { deb != nil }

    // MARK: - Checking

    /// Fetches the releases once per launch. Drafts and pre-releases are ignored, as in Caz and Dopamine.
    func check() {
        guard !checking, !checked else { return }
        checking = true
        var request = URLRequest(url: Self.apiURL, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            let releases = data.flatMap { Self.parse($0) }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.checking = false
                guard let releases = releases else { return }   // offline or rate limited: try next launch
                self.checked = true
                let package = Self.installedPackageVersion().map { Self.versionParts($0) }
                self.choose(from: releases, app: Self.versionParts(Bundle.main.shortVersion), package: package)
            }
        }.resume()
    }

    /// Each part comes from the newest release that has it, if that's newer than what's installed. A tweak that isn't
    /// installed (or whose version can't be read) counts as older than any release.
    func choose(from releases: [SpeakaRelease], app: [Int], package: [Int]?) {
        let newest = releases.sorted { Self.compare($0.version, $1.version) > 0 }
        if let release = newest.first(where: { $0.tipaURL != nil }), Self.compare(release.version, app) > 0 {
            tipa = release
        } else {
            tipa = nil
        }
        if let release = newest.first(where: { $0.debURL != nil }),
           package.map({ Self.compare(release.version, $0) > 0 }) ?? true {
            deb = release
        } else {
            deb = nil
        }
        let oldest = package.map { Self.compare($0, app) < 0 ? $0 : app } ?? app
        updates = newest.filter { Self.compare($0.version, oldest) > 0 }
    }

    private struct APIRelease: Decodable {
        struct Asset: Decodable {
            let name: String
            let browserDownloadUrl: String
        }
        let id: Int
        let tagName: String
        let name: String?
        let body: String?
        let draft: Bool
        let prerelease: Bool
        let assets: [Asset]
    }

    static func parse(_ data: Data) -> [SpeakaRelease]? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let list = try? decoder.decode([APIRelease].self, from: data) else { return nil }
        return list.compactMap { release -> SpeakaRelease? in
            if release.draft || release.prerelease { return nil }
            let version = versionParts(release.tagName)
            if version.isEmpty { return nil }
            let title = release.name.flatMap { $0.isEmpty ? nil : $0 } ?? release.tagName
            return SpeakaRelease(id: release.id, name: title, body: release.body ?? "", version: version,
                                 tipaURL: link(in: release.assets, ending: ".tipa") ?? link(in: release.assets, ending: ".ipa"),
                                 debURL: link(in: release.assets, ending: ".deb"))
        }
    }

    private static func link(in assets: [APIRelease.Asset], ending suffix: String) -> URL? {
        guard let asset = assets.first(where: { $0.name.lowercased().hasSuffix(suffix) }) else { return nil }
        return URL(string: asset.browserDownloadUrl)
    }

    /// "v9.3.57" -> [9, 3, 57]
    static func versionParts(_ text: String) -> [Int] {
        text.split(whereSeparator: { !$0.isASCII || !$0.isNumber }).compactMap { Int($0) }
    }

    static func compare(_ a: [Int], _ b: [Int]) -> Int {
        for index in 0..<max(a.count, b.count) {
            let x = index < a.count ? a[index] : 0
            let y = index < b.count ? b[index] : 0
            if x != y { return x < y ? -1 : 1 }
        }
        return 0
    }

    // MARK: - What's installed

    /// The installed tweak's version, from dpkg's database (a rootless jailbreak keeps it under /var/jb).
    static func installedPackageVersion() -> String? {
        for path in ["/var/jb/var/lib/dpkg/status", "/var/lib/dpkg/status"] {
            guard let status = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
            return packageVersion(in: status, package: packageID)
        }
        return nil
    }

    /// The Version of `package` in a dpkg status file, if it's installed (not just remembered after a removal).
    static func packageVersion(in status: String, package: String) -> String? {
        for stanza in status.components(separatedBy: "\n\n") {
            var name: String? = nil
            var version: String? = nil
            var installed = false
            for line in stanza.split(separator: "\n") {
                if line.hasPrefix("Package: ") {
                    name = String(line.dropFirst(9)).trimmingCharacters(in: .whitespaces)
                } else if line.hasPrefix("Version: ") {
                    version = String(line.dropFirst(9)).trimmingCharacters(in: .whitespaces)
                } else if line.hasPrefix("Status: ") {
                    installed = line.hasSuffix(" installed")
                }
            }
            if name == package {
                return installed ? version : nil
            }
        }
        return nil
    }

    // MARK: - Downloading and installing

    /// Downloads what's newer (the .deb, then the .tipa) and hands both to the update helper. If only the app was due
    /// and installing it fails, TrollStore is asked to install it from its link instead, as in Caz.
    func startDownload() {
        guard isAvailable, !isBusy else { return }
        var parts: [Part] = []
        if let url = deb?.debURL { parts.append(Part(url: url, name: "Speaka-update.deb")) }
        if let url = tipa?.tipaURL { parts.append(Part(url: url, name: "Speaka-update.tipa")) }
        stage = .downloading
        progress = 0
        target = 0
        partCount = parts.count
        partsDone = 0
        startTimer()
        download(parts, saved: [])
    }

    private func download(_ parts: [Part], saved: [URL]) {
        guard let part = parts.first else {
            install(saved)
            return
        }
        let downloadTask = URLSession.shared.downloadTask(with: part.url) { [weak self] location, response, error in
            var file: URL? = nil
            var problem = error?.localizedDescription
            if let location = location, error == nil {
                if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                    problem = "GitHub answered with error \(http.statusCode)."
                } else {
                    let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                    let destination = folder.appendingPathComponent(part.name)
                    try? FileManager.default.removeItem(at: destination)
                    do {
                        try FileManager.default.moveItem(at: location, to: destination)
                        file = destination
                    } catch {
                        problem = error.localizedDescription
                    }
                }
            }
            let finished = file
            let reason = problem
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard let finished = finished else {
                    self.fail(reason ?? "The download didn't finish.")
                    return
                }
                self.partsDone += 1
                self.download(Array(parts.dropFirst()), saved: saved + [finished])
            }
        }
        task = downloadTask
        downloadTask.resume()
    }

    /// The helper installs the .deb first; if that fails, nothing else changes. Then come the .tipa and the respring,
    /// which close Speaka, so they run on in the helper after Speaka is gone.
    private func install(_ files: [URL]) {
        target = 1
        stage = .installing
        var args = ["install-update"]
        for file in files {
            args += [file.pathExtension.lowercased() == "deb" ? "--deb" : "--tipa", file.path]
        }
        let helper = Bundle.main.bundlePath + "/speakahelper"
        let withTweak = files.contains { $0.pathExtension.lowercased() == "deb" }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var errors: [String] = []
            let status = SpeakaSpawnRoot(helper, args) { line in
                if line.hasPrefix("ERR\t") { errors.append(String(line.dropFirst(4))) }
            }
            let reasons = errors
            DispatchQueue.main.async {
                self?.installed(status: status, errors: reasons, withTweak: withTweak)
            }
        }
    }

    /// The helper's exit codes: 0 done, 3 not root, 4 a file is missing, 5 TrollStore's installer wasn't found, 6 it
    /// couldn't start, 7 it failed, 8 dpkg failed, 9 no dpkg, 10 the last step couldn't start; -1 the helper didn't start.
    private func installed(status: Int32, errors: [String], withTweak: Bool) {
        if withTweak {
            if status == 0 {
                return              // still "Installing update...": the respring closes Speaka
            }
            fail(errors.last ?? "The update didn't install (code \(status)).")
            return
        }
        if status == 0 {
            stage = .installed
        } else {
            openInTrollStore()
        }
    }

    /// 60 times a second: read the real progress and move the circle a quarter of the way there.
    private func startTimer() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] tick in
            guard let self = self else {
                tick.invalidate()
                return
            }
            if self.progress >= 0.99 {
                self.progress = 1
                tick.invalidate()
                return
            }
            if let task = self.task, task.countOfBytesExpectedToReceive > 0 {
                let real = Double(task.countOfBytesReceived) / Double(task.countOfBytesExpectedToReceive)
                let overall = (Double(self.partsDone) + min(1, real)) / Double(max(1, self.partCount))
                self.target = max(self.target, overall)
            }
            if self.progress < self.target {
                self.progress += (self.target - self.progress) * 0.25
            }
        }
    }

    private func fail(_ message: String) {
        timer?.invalidate()
        stage = .failed(message)
    }

    /// TrollStore installs from a link with apple-magnifier://install?url=, the same hand-off Caz and Dopamine use.
    /// Without TrollStore, the releases page opens instead.
    func openInTrollStore() {
        guard let url = tipa?.tipaURL,
              let link = URL(string: "apple-magnifier://install?url=" + url.absoluteString) else {
            UIApplication.shared.open(Self.releasesPage)
            return
        }
        UIApplication.shared.open(link, options: [:]) { opened in
            if !opened { UIApplication.shared.open(SpeakaUpdater.releasesPage) }
        }
    }

    /// Back to the changelog when the window closes, unless something is still running.
    func resetIfIdle() {
        if !isBusy { stage = .changelog }
    }
}
