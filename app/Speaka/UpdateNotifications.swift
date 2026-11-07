//
//  UpdateNotifications.swift
//  Speaka
//
//  Settings > Update notifications (v9.7; v9.9.3 made dependable): a notification when a new release is ready.
//

import BackgroundTasks
import Foundation
import UserNotifications

/// Three ways a release is found, one rule - each release announced once, whichever finds it first (the record is
/// shared, SPEAKA_KEY_UPDATE_ANNOUNCED):
/// - SpeakaUpdateCheck, a launchd job, every 15 minutes whether Speaka runs or not: the dependable one.
/// - Speaka's own background check, when iOS chooses to run it (often hours apart, never once Speaka was swiped away).
/// - While Speaka is open: every 2 minutes, and each time it comes to the front (SpeakaUpdater).
/// A release is announced only while iOS allows Speaka's notifications, and counts as announced only once iOS has
/// accepted the request - so a release found while they're off is announced when they're back on. Tapping the
/// notification opens the update (SpeakaNotificationDelegate).
enum UpdateNotifications {
    /// Listed in Info.plist under BGTaskSchedulerPermittedIdentifiers.
    static let taskID = "speaka.update-check"
    /// While Speaka is open (SpeakaUpdater.watch). Each check is conditional, so an unchanged list costs nothing
    /// against GitHub's hourly limit.
    static let interval: TimeInterval = 120
    /// What Speaka asks iOS for in the background. iOS takes it as "not before", and decides.
    static let backgroundInterval: TimeInterval = 15 * 60

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

    /// iOS shows Speaka's notifications: authorized, provisional or ephemeral.
    static func shows(_ status: UNAuthorizationStatus) -> Bool {
        switch status {
        case .authorized, .provisional, .ephemeral: return true
        default: return false
        }
    }

    /// Whether iOS lets Speaka notify now (Settings > Notifications > Speaka can be turned off at any time).
    static func allowed(_ completion: @escaping (Bool) -> Void) {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let ok = UpdateNotifications.shows(settings.authorizationStatus)
            DispatchQueue.main.async { completion(ok) }
        }
    }

    /// Asks iOS to wake Speaka in the background in 15 minutes, or when it allows. If Background App Refresh is off
    /// for Speaka (or Low Power Mode is on) iOS refuses; SpeakaUpdateCheck still checks.
    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: UpdateNotifications.taskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: UpdateNotifications.backgroundInterval)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // Refused for now; the launchd check doesn't depend on it.
        }
    }

    /// The background wake-up: the next one is asked for first, so one failed check doesn't end the chain.
    static func backgroundCheck() async {
        guard SPTweakClient.updateNotificationsEnabled() else { return }
        UpdateNotifications.schedule()
        await UpdateNotifications.check()
    }

    /// One check: the newest release that carries the app, if it's newer than this app and wasn't announced - and
    /// only while iOS allows Speaka's notifications.
    static func check() async {
        let releases: [SpeakaRelease]? = await withCheckedContinuation { continuation in
            SpeakaUpdater.fetchReleases { result in continuation.resume(returning: try? result.get()) }
        }
        guard let releases else { return }
        let app = SpeakaUpdater.versionParts(Bundle.main.shortVersion)
        let ready = releases.filter { $0.tipaURL != nil }
        guard let newest = ready.max(by: { SpeakaUpdater.compare($0.version, $1.version) < 0 }),
              SpeakaUpdater.compare(newest.version, app) > 0,
              SPTweakClient.notifiedVersion() != newest.name else { return }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard UpdateNotifications.shows(settings.authorizationStatus) else { return }   // kept for when they're allowed
        let content = UNMutableNotificationContent()
        content.title = "Speaka \(newest.name) is ready"
        content.body = "Tap to update."
        content.sound = .default
        let note = UNNotificationRequest(identifier: "update-\(newest.name)", content: content, trigger: nil)
        do {
            try await UNUserNotificationCenter.current().add(note)
            SPTweakClient.setNotifiedVersion(newest.name)   // only once iOS has accepted it
        } catch {
            // Not now: the next check tries again.
        }
    }
}

/// v9.9.3: Speaka's notifications show as banners while it's open, and tapping an update notification - Speaka's own
/// or SpeakaUpdateCheck's - opens the update window. Installed at launch (SpeakaApp), so a tap that launches Speaka
/// isn't missed.
final class SpeakaNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let shared = SpeakaNotificationDelegate()
    static let openUpdate = Notification.Name("speaka.open-update")
    /// A tap that came before the main screen was up (it launched Speaka); the main screen takes it when it appears.
    static var openRequested = false

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.notification.request.identifier.hasPrefix("update-") {
            DispatchQueue.main.async {
                SpeakaNotificationDelegate.openRequested = true
                NotificationCenter.default.post(name: SpeakaNotificationDelegate.openUpdate, object: nil)
            }
        }
        completionHandler()
    }
}
