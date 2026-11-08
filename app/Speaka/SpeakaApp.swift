//
//  SpeakaApp.swift
//  Speaka
//
//  The app's entry point.
//

import SwiftUI
import UserNotifications

@main
struct SpeakaApp: App {
    init() {
        // v9.9.3: banners while open, and a tap on an update notification opens the update window.
        UNUserNotificationCenter.current().delegate = SpeakaNotificationDelegate.shared
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(.dark)
        }
        // Settings > Update notifications: iOS wakes Speaka in the background when it chooses (UpdateNotifications).
        .backgroundTask(.appRefresh(UpdateNotifications.taskID)) {
            await UpdateNotifications.backgroundCheck()
        }
    }
}
