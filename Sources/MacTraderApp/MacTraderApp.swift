import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Set the bundle icon at runtime too, so the Dock does not keep a
        // generic executable icon while LaunchServices refreshes metadata.
        if let iconURL = Bundle.main.url(forResource: "NovaTrade", withExtension: "icns"),
           let icon = NSImage(contentsOf: iconURL) {
            NSApp.applicationIconImage = icon
        }
        // A bare `swift run mac-trader` executable starts as a background
        // process; promote it to a regular foreground app.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct MacTraderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup("NovaTrade") {
            DashboardView()
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1480, height: 900)
    }
}
