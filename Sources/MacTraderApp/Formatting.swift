import Foundation

extension Decimal {
    var doubleValue: Double { NSDecimalNumber(decimal: self).doubleValue }
}

private func decimalFormatter(fractionDigits: Int) -> NumberFormatter {
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.numberStyle = .decimal
    formatter.usesGroupingSeparator = true
    formatter.minimumFractionDigits = fractionDigits
    formatter.maximumFractionDigits = fractionDigits
    return formatter
}

private let twoDecimalPriceFormatter = decimalFormatter(fractionDigits: 2)
private let threeDecimalPriceFormatter = decimalFormatter(fractionDigits: 3)
private let smallPriceFormatter: NumberFormatter = {
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.numberStyle = .decimal
    formatter.usesSignificantDigits = true
    formatter.minimumSignificantDigits = 3
    formatter.maximumSignificantDigits = 6
    return formatter
}()
private let usdFormatter: NumberFormatter = {
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.numberStyle = .currency
    formatter.currencyCode = "USD"
    formatter.currencySymbol = "$"
    formatter.minimumFractionDigits = 2
    formatter.maximumFractionDigits = 2
    return formatter
}()

/// Sub-unit prices keep significant digits; larger prices use a fixed scale.
func formatPrice(_ value: Double) -> String {
    let magnitude = abs(value)
    let formatter = magnitude > 0 && magnitude < 1 ? smallPriceFormatter
        : magnitude < 10 ? threeDecimalPriceFormatter : twoDecimalPriceFormatter
    return formatter.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value)
}

func formatPrice(_ value: Decimal) -> String { formatPrice(value.doubleValue) }

func formatUSD(_ value: Decimal) -> String {
    let number = value.doubleValue
    return usdFormatter.string(from: NSNumber(value: number)) ?? String(format: "$%.2f", number)
}

func formatSignedUSD(_ value: Decimal) -> String {
    let number = value.doubleValue
    return String(format: "%@$%.2f", number >= 0 ? "+" : "-", abs(number))
}

func formatCompact(_ value: Double) -> String {
    if value >= 1_000_000_000 { return String(format: "%.2fB", value / 1_000_000_000) }
    if value >= 1_000_000 { return String(format: "%.2fM", value / 1_000_000) }
    if value >= 1_000 { return String(format: "%.1fK", value / 1_000) }
    return String(format: "%.2f", value)
}

func formatQuantity(_ value: Decimal) -> String { String(format: "%.4f", value.doubleValue) }

func formatSigned(_ value: Decimal) -> String { String(format: "%+.2f", value.doubleValue) }

func formatSignedPercent(_ value: Double) -> String { String(format: "%+.2f%%", value) }

private let shortTimestampFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "MM-dd HH:mm"
    // Candle/risk timestamps are absolute instants.  Their day boundaries are
    // decided by the service in UTC, but operator-facing labels stay local.
    formatter.timeZone = .autoupdatingCurrent
    return formatter
}()

/// Compact local time for rail rows ("10-02 12:30").
func formatShortTimestamp(_ date: Date) -> String { shortTimestampFormatter.string(from: date) }

private let localTimeFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = .current
    formatter.dateStyle = .none
    formatter.timeStyle = .short
    formatter.timeZone = .autoupdatingCurrent
    return formatter
}()

/// Localized clock time for fills and runtime log rows.
func formatLocalTime(_ date: Date) -> String { localTimeFormatter.string(from: date) }

private let localClockFormatter: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "HH:mm:ss"
    formatter.timeZone = .autoupdatingCurrent
    return formatter
}()

/// 24-hour local clock used by the live candle status line.
func formatLocalClock(_ date: Date) -> String { localClockFormatter.string(from: date) }

/// OKX contract counts are whole numbers for most instruments; keep the
/// fraction only when the lot size actually produces one.
func formatContracts(_ value: Decimal) -> String {
    let number = value.doubleValue
    return number.rounded() == number ? String(format: "%.0f", number) : String(format: "%.4f", number)
}

/// "1%" for whole percentages, "12.5%" otherwise.
func formatPercent(_ value: Decimal) -> String {
    let number = value.doubleValue
    return number.rounded() == number ? String(format: "%.0f%%", number) : String(format: "%.1f%%", number)
}

func formatLeverage(_ value: Double) -> String {
    value.rounded() == value ? String(format: "%.0f", value) : String(format: "%.1f", value)
}
