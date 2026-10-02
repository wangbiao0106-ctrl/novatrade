import Foundation

/// One vocabulary for exchange order states.
///
/// The service used to carry a literal list per call site, so adding a status
/// meant finding and updating every one of them. The lists here are the only
/// definitions; `OrderSnapshot` and the trading service both read them.
public enum OrderLifecycle {
    /// OKX states in which the order is done and cannot rest on the book any
    /// more. `filled` is included because a fully filled order is finished;
    /// callers that care about the difference between "ended empty" and
    /// "ended with a position" use `unfilledTerminalStates` instead.
    public static let terminalStates: Set<String> = [
        "filled", "closed", "canceled", "cancelled", "rejected", "expired", "failed", "mmp_canceled", "order_failed"
    ]

    /// Terminal states that mean no size was executed, so the order left no
    /// position behind. `mmp_canceled` is OKX's market-maker-protection
    /// cancellation; `order_failed` is the command layer's spelling of a
    /// rejection. A caller must still check the reported fill quantity before
    /// treating one of these as proof that nothing was executed.
    public static let unfilledTerminalStates: Set<String> = [
        "canceled", "cancelled", "rejected", "expired", "failed", "mmp_canceled", "order_failed"
    ]

    /// States in which the order can still rest on the book and fill later.
    public static let restingStates: Set<String> = ["live", "partially_filled", "partiallyfilled"]

    /// Normalizes the spellings OKX and the command layer use for the same
    /// state, so a state check cannot miss an order because of a hyphen.
    public static func normalized(_ status: String) -> String {
        status.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: "-", with: "_")
    }

    public static func isTerminal(_ status: String) -> Bool {
        terminalStates.contains(normalized(status))
    }

    public static func isUnfilledTerminal(_ status: String) -> Bool {
        unfilledTerminalStates.contains(normalized(status))
    }

    public static func isResting(_ status: String) -> Bool {
        restingStates.contains(normalized(status))
    }
}

public extension OrderSnapshot {
    /// True when this order is finished and cannot fill again.
    var isTerminal: Bool { OrderLifecycle.isTerminal(status) }

    /// True when this order is resting on the book and can still fill.
    var isResting: Bool { OrderLifecycle.isResting(status) }

    /// True when the order ended without executing any size. Requires a
    /// reported zero fill: a cancelled order can still have filled partly,
    /// and a missing fill report is not evidence of no fill.
    var endedUnfilled: Bool {
        OrderLifecycle.isUnfilledTerminal(status) && filledQuantity == 0
    }
}
