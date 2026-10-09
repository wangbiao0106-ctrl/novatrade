import Foundation
import Testing
import TradingDomain

private func riskContractDecode<Value: Decodable>(_ type: Value.Type, _ json: String) throws -> Value {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    return try decoder.decode(type, from: Data(json.utf8))
}

private func riskContractEncode<Value: Encodable>(_ value: Value) throws -> Data {
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    return try encoder.encode(value)
}

private func riskContractObject(_ json: String) throws -> NSDictionary {
    try #require(JSONSerialization.jsonObject(with: Data(json.utf8)) as? NSDictionary)
}

private func riskContractObject<Value: Encodable>(_ value: Value) throws -> NSDictionary {
    try #require(JSONSerialization.jsonObject(with: riskContractEncode(value)) as? NSDictionary)
}

@Test("Legacy AI configuration ignores retired controls while retaining active settings")
func aiRiskLegacyConfigurationRetiredFields() throws {
    let config = try riskContractDecode(AIConfig.self, """
    {"strategyID":"codex", "provider":"codex", "enabled":true,
     "mode":"paper-active", "allowedInstruments":["SATS-USDT-SWAP"],
     "minimumConfidence":0.83, "decisionIntervalSeconds":45.5,
     "maxDailyLosses":3, "marginPerOrderUSD":250,
     "cooldownSeconds":60, "escalationModel":"gpt-6.1-sol",
     "escalationReasoningEffort":"high"}
    """)
    #expect(config.enabled)
    #expect(config.mode == .paperActive)
    #expect(config.allowedInstruments == ["SATS-USDT-SWAP"])
    #expect(config.minimumConfidence == 0.83)
    #expect(config.decisionIntervalSeconds == 45.5)
    #expect(config.maxDailyLosses == 3)
    #expect(config.marginPerOrderUSD == 250)
    let encoded = try riskContractObject(config)
    for retiredKey in ["cooldownSeconds", "escalationModel", "escalationReasoningEffort"] {
        #expect(encoded[retiredKey] == nil)
    }
    let restored = try riskContractDecode(AIConfig.self, String(decoding: riskContractEncode(config), as: UTF8.self))
    #expect(restored == config)
}

@Test("AI configuration defaults match the server risk contract and encode the current field set")
func aiRiskConfigurationDefaults() throws {
    let config = try riskContractDecode(AIConfig.self, "{\"strategyID\":\"codex\",\"provider\":\"codex\"}")
    #expect(config.decisionIntervalSeconds == 600)
    #expect(config.snapshotMaxAgeSeconds == 90)
    #expect(config.stopLossCooldownSeconds == 14_400)
    #expect(config.recentStopLossWindowSeconds == 3_600)
    #expect(config.recentStopLossLimit == 2)
    #expect(config.requireStopLoss)
    #expect(config == AIConfig(strategyID: .codex, provider: .codex))
    let expected = try riskContractObject("""
    {"strategyID":"codex", "provider":"codex", "enabled":false,
     "mode":"disabled", "allowedInstruments":[], "minimumConfidence":0.65,
     "decisionIntervalSeconds":600, "cliTimeoutSeconds":90,
     "snapshotMaxAgeSeconds":90, "maxOutputBytes":1000000,
     "maxConsecutiveFailures":3, "allowOpen":true, "allowClose":true,
     "allowCancel":true, "requireStopLoss":true, "maxDailyOrders":20,
     "maxDailyLosses":5, "marginPerOrderUSD":500, "maxLeverage":5,
     "stopLossCooldownSeconds":14400, "recentStopLossWindowSeconds":3600,
     "recentStopLossLimit":2}
    """)
    #expect(try riskContractObject(config).isEqual(expected))
    #expect(try riskContractDecode(AIConfig.self, String(decoding: riskContractEncode(config), as: UTF8.self)) == config)
}

@Test("Custom scan interval, snapshot and stop-loss controls survive configuration round trips")
func aiRiskConfigurationCustomControls() throws {
    let config = try riskContractDecode(AIConfig.self, """
    {"decisionIntervalSeconds":900.25, "snapshotMaxAgeSeconds":180.5,
     "stopLossCooldownSeconds":7200.25,
     "recentStopLossWindowSeconds":1200.5, "recentStopLossLimit":4,
     "requireStopLoss":true}
    """)
    #expect(config.decisionIntervalSeconds == 900.25)
    #expect(config.snapshotMaxAgeSeconds == 180.5)
    #expect(config.stopLossCooldownSeconds == 7_200.25)
    #expect(config.recentStopLossWindowSeconds == 1_200.5)
    #expect(config.recentStopLossLimit == 4)
    let encoded = try riskContractObject(config)
    #expect(encoded["decisionIntervalSeconds"] as? Double == 900.25)
    #expect(encoded["snapshotMaxAgeSeconds"] as? Double == 180.5)
    #expect(encoded["stopLossCooldownSeconds"] as? Double == 7_200.25)
    #expect(encoded["recentStopLossWindowSeconds"] as? Double == 1_200.5)
    #expect(encoded["recentStopLossLimit"] as? Int == 4)
    #expect(try riskContractDecode(AIConfig.self, String(decoding: riskContractEncode(config), as: UTF8.self)) == config)
}

@Test("Risk configuration patches transmit only the explicitly changed controls")
func aiRiskPartialPatchShape() throws {
    let patch = AIPatch(
        snapshotMaxAgeSeconds: 240,
        stopLossCooldownSeconds: 0,
        recentStopLossWindowSeconds: 900,
        recentStopLossLimit: 3
    )
    let expected = try riskContractObject("""
    {"snapshotMaxAgeSeconds":240, "stopLossCooldownSeconds":0,
     "recentStopLossWindowSeconds":900, "recentStopLossLimit":3}
    """)
    #expect(try riskContractObject(patch).isEqual(expected))
    #expect(try riskContractDecode(AIPatch.self, String(decoding: riskContractEncode(patch), as: UTF8.self)) == patch)
    #expect(try riskContractObject(AIPatch()).isEqual(NSDictionary()))
}

private let riskContractLevelsJSON = """
[{"price":0.00000000609876546,"quantityPercent":37.5},
 {"price":0.00000000543210987,"quantityPercent":62.5}]
"""

private func riskContractAssessmentJSON(levelsField: String) -> String {
    """
    {"instrumentID":"SATS-USDT-SWAP", "direction":"short",
     "winRate":0.58, "riskRewardRatio":2.4, "limitPrice":0.0000000087654321,
     "stopLossPrice":0.0000000098765432, "takeProfitPrice":0.00000000543210987,
     "confidence":0.8,
     "entryEligible":true, "unmetConditions":[], "reason":"等待回抽入场。"\(levelsField)}
    """
}

private func riskContractDecisionJSON(levelsField: String) -> String {
    """
    {"schemaVersion":1, "decisionId":"staged-short", "snapshotId":"snapshot-staged",
     "action":"open", "instrumentID":"SATS-USDT-SWAP", "direction":"short",
     "orderType":"limit", "limitPrice":0.0000000087654321,
     "stopLossPrice":0.0000000098765432, "takeProfitPrice":0.00000000543210987,
     "confidence":0.8, "winRate":0.58,
     "riskRewardRatio":2.4, "leverage":3, "validUntil":"2026-10-09T08:00:00Z",
     "reasonCode":"PULLBACK_ENTRY", "reason":"按分批计划止盈。",
     "assessments":[\(riskContractAssessmentJSON(levelsField: levelsField))]\(levelsField)}
    """
}

@Test("Staged take-profit targets retain exact small prices and allocation in decisions, status and audit")
func aiRiskStagedTakeProfitRoundTrips() throws {
    let levelsField = ", \"takeProfitLevels\":\(riskContractLevelsJSON)"
    let decisionJSON = riskContractDecisionJSON(levelsField: levelsField)
    let legacyJSON = decisionJSON.replacingOccurrences(of: "\"schemaVersion\":1", with: "\"schemaVersion\":1,\"riskBudgetPercent\":2")
    let decision = try riskContractDecode(AIDecision.self, legacyJSON)
    let expectedLevels = [
        AITakeProfitLevel(price: try #require(Decimal(string: "0.00000000609876546")), quantityPercent: 37.5),
        AITakeProfitLevel(price: try #require(Decimal(string: "0.00000000543210987")), quantityPercent: 62.5)
    ]
    #expect(decision.takeProfitLevels == expectedLevels)
    #expect(decision.assessments.first?.takeProfitLevels == expectedLevels)
    #expect(decision.takeProfitPrice == expectedLevels.last?.price)
    #expect(decision.assessments.first?.takeProfitPrice == expectedLevels.last?.price)
    // The exact wire shape uses named price/quantityPercent fields and never
    // re-emits the retired risk budget from a historical model response.
    #expect(try riskContractObject(decision).isEqual(riskContractObject(decisionJSON)))
    let encodedDecision = try riskContractEncode(decision)
    #expect(try riskContractDecode(AIDecision.self, String(decoding: encodedDecision, as: UTF8.self)) == decision)

    let status = try riskContractDecode(AIStatus.self, "{\"lastDecision\":\(decisionJSON)}")
    #expect(status.lastDecision?.takeProfitLevels == expectedLevels)
    #expect(status.lastDecision?.assessments.first?.takeProfitLevels == expectedLevels)
    #expect(try riskContractDecode(AIStatus.self, String(decoding: riskContractEncode(status), as: UTF8.self)) == status)

    let audit = try riskContractDecode(AIAuditRecord.self, """
    {"type":"decision", "accepted":false, "reason":"snapshot expired",
     "decision":\(decisionJSON), "rawDecision":\(legacyJSON)}
    """)
    #expect(audit.decision?.takeProfitLevels == expectedLevels)
    #expect(audit.rawDecision?.takeProfitLevels == expectedLevels)
    #expect(audit.rawDecision?.assessments.first?.takeProfitLevels == expectedLevels)
    #expect(try riskContractDecode(AIAuditRecord.self, String(decoding: riskContractEncode(audit), as: UTF8.self)) == audit)
}

@Test("Historical decisions and assessments accept omitted or null staged targets", arguments: ["", ", \"takeProfitLevels\":null"])
func aiRiskHistoricalTakeProfitCompatibility(levelsField: String) throws {
    let decision = try riskContractDecode(AIDecision.self, riskContractDecisionJSON(levelsField: levelsField))
    #expect(decision.takeProfitLevels == nil)
    #expect(decision.assessments.first?.takeProfitLevels == nil)
    let encoded = try riskContractObject(decision)
    #expect(encoded["takeProfitLevels"] == nil)
    let assessments = try #require(encoded["assessments"] as? [NSDictionary])
    #expect(assessments.first?["takeProfitLevels"] == nil)
    #expect(try riskContractDecode(AIDecision.self, String(decoding: riskContractEncode(decision), as: UTF8.self)) == decision)
}

@Test("Malformed staged targets fail decoding instead of disappearing from an audit", arguments: [
    "[{\"price\":0.00000000609876546}]",
    "[{\"quantityPercent\":100}]",
    "[{\"price\":true,\"quantityPercent\":100}]",
    "[{\"price\":0.00000000609876546,\"quantityPercent\":\"100\"}]",
    "[{\"price\":0.00000000609876546,\"quantityPercent\":null}]",
    "{\"price\":0.00000000609876546,\"quantityPercent\":100}"
])
func aiRiskMalformedTakeProfitTargets(levelsJSON: String) {
    let levelsField = ", \"takeProfitLevels\":\(levelsJSON)"
    #expect(throws: DecodingError.self) {
        try riskContractDecode(AIDecision.self, riskContractDecisionJSON(levelsField: levelsField))
    }
    #expect(throws: DecodingError.self) {
        try riskContractDecode(AIInstrumentAssessment.self, riskContractAssessmentJSON(levelsField: levelsField))
    }
}
