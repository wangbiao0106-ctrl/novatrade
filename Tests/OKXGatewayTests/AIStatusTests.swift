import Foundation
import Testing
import TradingDomain

private let historicalHoldJSON = """
{
  "schemaVersion": 1,
  "decisionId": "decision-hold",
  "snapshotId": "snapshot-old",
  "action": "hold",
  "instrumentID": null,
  "confidence": 0.99,
  "validUntil": "2026-10-05T15:37:04Z",
  "reasonCode": "INCOMPLETE_SNAPSHOT",
  "reason": "订单簿数据缺失，继续观望。"
}
"""

private func decodeAIStatus(_ json: String) throws -> AIStatus {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    return try decoder.decode(AIStatus.self, from: Data(json.utf8))
}

@Test("Historical holds without a recorded scope do not inherit the current observation pool")
func aiStatusHistoricalHoldScope() throws {
    let status = try decodeAIStatus("""
    {
      "lastDecision": \(historicalHoldJSON),
      "observedInstruments": ["ETH-USDT-SWAP"]
    }
    """)
    #expect(status.lastDecision?.action == .hold)
    #expect(status.lastDecision?.instrumentID == nil)
    #expect(status.lastDecision?.confidence == 0.99)
    #expect(status.lastDecision?.assessments == [])
    #expect(status.lastDecisionInstruments.isEmpty)
    #expect(status.lastDecisionFreshness == nil)
    #expect(status.observedInstruments == ["ETH-USDT-SWAP"])
}

@Test("A decision keeps its own contract list when the observation pool changes")
func aiStatusDecisionScopeRoundTrip() throws {
    let status = try decodeAIStatus("""
    {
      "lastDecision": \(historicalHoldJSON),
      "observedInstruments": ["ETH-USDT-SWAP"],
      "lastDecisionInstruments": ["BTC-USDT-SWAP", "DOGE-USDT-SWAP"]
    }
    """)
    #expect(status.lastDecisionInstruments == ["BTC-USDT-SWAP", "DOGE-USDT-SWAP"])
    #expect(status.lastDecision?.reason == "订单簿数据缺失，继续观望。")
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    let encoded = try encoder.encode(status)
    let restored = try decodeAIStatus(String(decoding: encoded, as: UTF8.self))
    #expect(restored == status)
}

@Test("A single-contract hold retains its contract independently of the full pool")
func aiStatusSingleContractHold() throws {
    let singleHold = historicalHoldJSON.replacingOccurrences(of: "\"instrumentID\": null", with: "\"instrumentID\": \"BTC-USDT-SWAP\"")
    let status = try decodeAIStatus("""
    {
      "lastDecision": \(singleHold),
      "lastDecisionInstruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    }
    """)
    #expect(status.lastDecision?.instrumentID == "BTC-USDT-SWAP")
    #expect(status.lastDecisionInstruments.count == 2)
}

@Test("Server freshness remains paired with the decision across UTC and local day boundaries")
func aiStatusServerFreshnessRoundTrip() throws {
    let status = try decodeAIStatus("""
    {
      "lastDecision": \(historicalHoldJSON),
      "lastDecisionFreshness": {
        "capturedAt": "2026-10-05T16:28:30Z",
        "evaluatedAt": "2026-10-05T16:28:49Z",
        "ageSeconds": 19,
        "maxAgeSeconds": 90,
        "isStale": false,
        "valid": true,
        "error": ""
      }
    }
    """)
    let freshness = try #require(status.lastDecisionFreshness)
    let capturedAt = try #require(freshness.capturedAt)
    let evaluatedAt = try #require(freshness.evaluatedAt)
    #expect(evaluatedAt.timeIntervalSince(capturedAt) == 19)
    #expect(freshness.ageSeconds == 19)
    #expect(freshness.maxAgeSeconds == 90)
    #expect(freshness.isStale == false)
    #expect(freshness.valid == true)
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    let encoded = try encoder.encode(status)
    #expect(try decodeAIStatus(String(decoding: encoded, as: UTF8.self)) == status)
}

@Test("An empty server freshness record remains compatible with status decoding")
func aiStatusEmptyFreshness() throws {
    let status = try decodeAIStatus("""
    {"lastDecisionFreshness": {}}
    """)
    #expect(status.lastDecisionFreshness?.ageSeconds == nil)
    #expect(status.lastDecisionFreshness?.isStale == nil)
}

private func decodeAIAudit(_ json: String) throws -> AIAuditRecord {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    return try decoder.decode(AIAuditRecord.self, from: Data(json.utf8))
}

private let neutralAssessmentJSON = """
{
  "instrumentID": "BTC-USDT-SWAP",
  "direction": "neutral",
  "winRate": null,
  "riskRewardRatio": null,
  "limitPrice": null,
  "stopLossPrice": null,
  "takeProfitPrice": null,
  "confidence": 0.88,
  "entryEligible": false,
  "unmetConditions": ["尚未形成明确方向", "无法可靠估计胜率和挂单价格"],
  "reason": "多空结构冲突，继续等待。"
}
"""

private let shortAssessmentJSON = """
{
  "instrumentID": "SATS-USDT-SWAP",
  "direction": "short",
  "winRate": 0.58,
  "riskRewardRatio": 2.4,
  "limitPrice": 0.0000000087654321,
  "stopLossPrice": 0.0000000098765432,
  "takeProfitPrice": 0.00000000609876546,
  "confidence": 0.8,
  "entryEligible": true,
  "unmetConditions": [],
  "reason": "等待回抽至建议挂单价。"
}
"""

private func holdWithAssessments(_ assessmentsJSON: String) -> String {
    historicalHoldJSON.replacingOccurrences(
        of: "\"reason\": \"订单簿数据缺失，继续观望。\"",
        with: "\"reason\": \"订单簿数据缺失，继续观望。\", \"assessments\": [\(assessmentsJSON)]"
    )
}

@Test("A hold retains all sixteen per-contract assessments and their original scope")
func aiStatusFullObservationAssessmentsRoundTrip() throws {
    let instruments = ["BOME", "BONK", "BTC", "DOGE", "MEME", "MEW", "MUBARAK", "NEIRO", "ONE", "PENGU", "PEPE", "PUMP", "SAND", "SATS", "SHIB", "STRK"].map { "\($0)-USDT-SWAP" }
    let rows = instruments.enumerated().map { index, instrument in
        let direction = index.isMultiple(of: 2) ? "long" : "short"
        return """
        {"instrumentID": "\(instrument)", "direction": "\(direction)",
         "winRate": 0.43, "riskRewardRatio": 1.8, "limitPrice": 0.01234567,
         "stopLossPrice": null, "takeProfitPrice": null, "confidence": 0.7,
         "entryEligible": false, "unmetConditions": ["胜率低于 45%", "盈亏比低于 2.0"],
         "reason": "信号质量未达到开仓门槛。"}
        """
    }.joined(separator: ",")
    let instrumentJSON = String(decoding: try JSONEncoder().encode(instruments), as: UTF8.self)
    let status = try decodeAIStatus("""
    {"lastDecision": \(holdWithAssessments(rows)),
     "lastDecisionInstruments": \(instrumentJSON),
     "observedInstruments": ["ETH-USDT-SWAP"]}
    """)
    let decision = try #require(status.lastDecision)
    #expect(decision.action == .hold)
    #expect(decision.assessments.map(\.instrumentID) == instruments)
    #expect(decision.assessments.count == 16)
    #expect(decision.assessments.first?.direction == .long)
    #expect(decision.assessments.last?.direction == .short)
    #expect(decision.assessments.allSatisfy { !$0.entryEligible && $0.unmetConditions.count == 2 })
    #expect(status.lastDecisionInstruments == instruments)
    #expect(status.observedInstruments == ["ETH-USDT-SWAP"])

    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    let restored = try decodeAIStatus(String(decoding: encoder.encode(status), as: UTF8.self))
    #expect(restored == status)
}

@Test("A neutral assessment preserves unknown metrics and explains why it cannot suggest an entry")
func aiStatusNeutralAssessmentUnknowns() throws {
    let status = try decodeAIStatus("""
    {"lastDecision": \(holdWithAssessments(neutralAssessmentJSON))}
    """)
    let row = try #require(status.lastDecision?.assessments.first)
    #expect(row.direction == .neutral)
    #expect(row.winRate == nil)
    #expect(row.riskRewardRatio == nil)
    #expect(row.limitPrice == nil)
    #expect(row.stopLossPrice == nil)
    #expect(row.takeProfitPrice == nil)
    #expect(row.entryEligible == false)
    #expect(row.unmetConditions == ["尚未形成明确方向", "无法可靠估计胜率和挂单价格"])
    #expect(row.reason == "多空结构冲突，继续等待。")
}

@Test("Small contract prices preserve Decimal precision through decoding and encoding")
func aiAssessmentSmallPricePrecisionRoundTrip() throws {
    let status = try decodeAIStatus("""
    {"lastDecision": \(holdWithAssessments(shortAssessmentJSON))}
    """)
    let row = try #require(status.lastDecision?.assessments.first)
    #expect(row.limitPrice == Decimal(string: "0.0000000087654321"))
    #expect(row.stopLossPrice == Decimal(string: "0.0000000098765432"))
    #expect(row.takeProfitPrice == Decimal(string: "0.00000000609876546"))
    #expect(row.winRate == 0.58)
    #expect(row.riskRewardRatio == 2.4)
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    let restored = try decodeAIStatus(String(decoding: encoder.encode(status), as: UTF8.self))
    #expect(restored == status)
}

@Test("A rejected opening audit retains the original intent and per-contract assessments on the safe hold")
func aiAuditRejectedOpenAssessmentsRoundTrip() throws {
    let assessments = "\(neutralAssessmentJSON), \(shortAssessmentJSON)"
    let safeHold = holdWithAssessments(assessments)
    let originalOpen = safeHold
        .replacingOccurrences(of: "\"action\": \"hold\"", with: "\"action\": \"open\"")
        .replacingOccurrences(of: "\"instrumentID\": null", with: "\"instrumentID\": \"SATS-USDT-SWAP\", \"direction\": \"short\", \"orderType\": \"limit\", \"limitPrice\": 0.0000000087654321")
    let audit = try decodeAIAudit("""
    {"at": "2026-10-05T15:37:05Z", "type": "decision", "accepted": false,
     "reason": "snapshot expired", "snapshotId": "snapshot-old",
     "decision": \(safeHold), "rawDecision": \(originalOpen)}
    """)
    #expect(audit.accepted == false)
    #expect(audit.reason == "snapshot expired")
    #expect(audit.decision?.action == .hold)
    #expect(audit.rawDecision?.action == .open)
    #expect(audit.rawDecision?.instrumentID == "SATS-USDT-SWAP")
    #expect(audit.decision?.assessments.map(\.instrumentID) == ["BTC-USDT-SWAP", "SATS-USDT-SWAP"])
    #expect(audit.rawDecision?.assessments == audit.decision?.assessments)
    #expect(audit.rawDecision?.limitPrice == Decimal(string: "0.0000000087654321"))
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    let restored = try decodeAIAudit(String(decoding: encoder.encode(audit), as: UTF8.self))
    #expect(restored == audit)
}

@Test("Historical audit records without a raw model response or assessments remain readable")
func aiAuditHistoricalCompatibility() throws {
    let audit = try decodeAIAudit("""
    {"type": "decision", "accepted": true, "decision": \(historicalHoldJSON)}
    """)
    #expect(audit.rawDecision == nil)
    #expect(audit.decision?.action == .hold)
    #expect(audit.decision?.assessments == [])
}
