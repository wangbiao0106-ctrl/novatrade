import SwiftUI

extension Color {
    static let appBackground = Color(red: 0.055, green: 0.065, blue: 0.08)
    static let sidebarBackground = Color(red: 0.065, green: 0.075, blue: 0.09)
    static let panelBackground = Color(red: 0.09, green: 0.102, blue: 0.12)
    /// Rising prices are green and falling prices red, following the US stock
    /// market convention used throughout the app.
    static let marketRise = Color(red: 0.18, green: 0.76, blue: 0.56)
    static let marketFall = Color(red: 0.94, green: 0.27, blue: 0.31)
}
