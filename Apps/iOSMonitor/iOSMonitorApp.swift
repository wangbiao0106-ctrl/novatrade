import SwiftUI

@main
struct iOSMonitorApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                ContentUnavailableView("等待 Mac 服务", systemImage: "desktopcomputer")
                    .navigationTitle("OKX Monitor")
            }
        }
    }
}
