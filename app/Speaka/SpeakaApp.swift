//
//  SpeakaApp.swift
//  Speaka
//
//  The app's entry point, as in Caz (Caz/CazApp.swift).
//

import SwiftUI

@main
struct SpeakaApp: App {
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
