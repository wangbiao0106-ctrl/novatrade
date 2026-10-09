import Foundation

public enum AIAssessmentDirection: String, Codable, CaseIterable, Sendable {
    case long
    case short
    case neutral
}

/// One staged take-profit target. The server validates that all target
/// quantity percentages sum to 100 for the position.
public struct AITakeProfitLevel: Codable, Equatable, Sendable {
    public let price: Decimal
    public let quantityPercent: Double

    public init(price: Decimal, quantityPercent: Double) {
        self.price = price; self.quantityPercent = quantityPercent
    }
}

/// A model's proposed setup for one observed contract. These prices and
/// eligibility describe an assessment, not an exchange order or its status.
public struct AIInstrumentAssessment: Codable, Equatable, Sendable, Identifiable {
    public var id: String { instrumentID }
    public let instrumentID: String
    public let direction: AIAssessmentDirection
    public let winRate: Double?
    public let riskRewardRatio: Double?
    public let limitPrice: Decimal?
    public let stopLossPrice: Decimal?
    public let takeProfitPrice: Decimal?
    public let takeProfitLevels: [AITakeProfitLevel]?
    public let confidence: Double
    public let entryEligible: Bool
    public let unmetConditions: [String]
    public let reason: String

    public init(instrumentID: String, direction: AIAssessmentDirection, winRate: Double? = nil, riskRewardRatio: Double? = nil, limitPrice: Decimal? = nil, stopLossPrice: Decimal? = nil, takeProfitPrice: Decimal? = nil, takeProfitLevels: [AITakeProfitLevel]? = nil, confidence: Double = 0, entryEligible: Bool = false, unmetConditions: [String] = [], reason: String = "") {
        self.instrumentID = instrumentID; self.direction = direction
        self.winRate = winRate; self.riskRewardRatio = riskRewardRatio
        self.limitPrice = limitPrice; self.stopLossPrice = stopLossPrice; self.takeProfitPrice = takeProfitPrice
        self.takeProfitLevels = takeProfitLevels
        self.confidence = confidence; self.entryEligible = entryEligible
        self.unmetConditions = unmetConditions; self.reason = reason
    }
}
