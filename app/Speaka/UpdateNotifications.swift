//
//  UpdateNotifications.swift
//  Speaka
//
//  Settings > Update notifications (v9.7): a notification when a new release is ready.
//

import BackgroundTasks
import Foundation
import UserNotifications

/// While Speaka is open, the release check runs every 2 minutes (SpeakaUpdater.watch). In the background, iOS decides:
/// Speaka asks to be woken every 2 minutes, and iOS does that when it chooses, often much less often, and never once
/// Speaka has been swiped away from the app switcher. A release counts as ready once it carries Speaka.tipa, which CI
/// attaches when the release is published, and each release is announced once.
enum UpdateNotifications {
    /// Listed in Info.plist under BGTaskSchedulerPermittedIdentifiers.
    static let taskID = "speaka.update-check"
    static let interval: TimeInterval = 120

    /// Turning it on asks iOS for permission to notify first; if that's refused, it stays off. `completion` gets
    /// whether it ended up on, on the main thread.
    static func setEnabled(_ on: Bool, completion: @escaping (Bool) -> Void) {
        guard on else {
            SPTweakClient.setUpdateNotificationsEnabled(false)
            BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: UpdateNotifications.taskID)
            completion(false)
            return
        }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async {
                SPTweakClient.setUpdateNotificationsEnabled(granted)
                if granted {
                    UpdateNotifications.schedule()
                }
                completion(granted)
            }
        }
    }

    /// Asks iOS to wake Speaka in the background in 2 minutes, or as soon after as it allows.
    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: UpdateNotifications.taskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: UpdateNotifications.interval)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// The background wake-up: the next one is asked for first, so one failed check doesn't end the chain.
    static func backgroundCheck() async {
        guard SPTweakClient.updateNotificationsEnabled() else { return }
        UpdateNotifications.schedule()
        await UpdateNotifications.check()
    }

    /// One check: the newest release that carries Speaka.tipa, if it's newer than this app and hasn't been announced.
    static func check() async {
        var request = URLRequest(url: SpeakaUpdater.apiURL, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        guard let result = try? await URLSession.shared.data(for: request),
              let releases = SpeakaUpdater.parse(result.0) else { return }
        let app = SpeakaUpdater.versionParts(Bundle.main.shortVersion)
        let ready = releases.filter { $0.tipaURL != nil }
        guard let newest = ready.max(by: { SpeakaUpdater.compare($0.version, $1.version) < 0 }),
              SpeakaUpdater.compare(newest.version, app) > 0,
              SPTweakClient.notifiedVersion() != newest.name else { return }
        let content = UNMutableNotificationContent()
        content.title = "Speaka \(newest.name) is ready"
        content.body = "Open Speaka and tap Update Available."
        content.sound = .default
        let note = UNNotificationRequest(identifier: "update-\(newest.name)", content: content, trigger: nil)
        do {
            try await UNUserNotificationCenter.current().add(note)
            SPTweakClient.setNotifiedVersion(newest.name)
        } catch {
            // Not allowed, or not now: the next check tries again.
        }
    }
}
