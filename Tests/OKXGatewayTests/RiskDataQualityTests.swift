import Foundation
import Testing
import TradingDomain

private func decodeRiskQualitySnapshot(_ qualityJSON: String? = nil) throws -> RiskSnapshot {
    let qualityField = qualityJSON.map { ", \"dataQuality\": \($0)" } ?? ""
    let json = """
    {
      "equity": 9400, "equityPeak": 10500, "dayStartEquity": 10000,
      "dailyPnLPercent": -6, "drawdownPercent": 10.47619,
      "killSwitch": true, "reason": "账户日内亏损达到 5%",
      "strategyCapitals": [], "globalNotionals": {}
      \(qualityField)
    }
    """
    return try JSONDecoder().decode(RiskSnapshot.self, from: Data(json.utf8))
}

private func roundTripRiskQualitySnapshot(_ snapshot: RiskSnapshot) throws -> RiskSnapshot {
    try JSONDecoder().decode(RiskSnapshot.self, from: JSONEncoder().encode(snapshot))
}

@Test("Legacy risk snapshots remain readable without quality metadata")
func legacyRiskSnapshotWithoutDataQuality() throws {
    let snapshot = try decodeRiskQualitySnapshot()
    #expect(snapshot.dataQuality == nil)
    #expect(snapshot.dailyPnLPercent == -6)
    #expect(snapshot.killSwitch)
    #expect(try roundTripRiskQualitySnapshot(snapshot) == snapshot)
}

@Test("Paper risk quality remains distinct from exchange daily PnL authority")
func paperRiskDataQualityRoundTrip() throws {
    let snapshot = try decodeRiskQualitySnapshot("""
    {"available": true, "equitySource": "paper"}
    """)
    let quality = try #require(snapshot.dataQuality)
    #expect(quality.available == true)
    #expect(quality.equitySource == "paper")
    #expect(quality.dailyPnLAuthoritative == nil)
    #expect(quality.accountRefreshError == nil)
    #expect(quality.accountRefreshRetryable == nil)
    #expect(try roundTripRiskQualitySnapshot(snapshot) == snapshot)
}

@Test("Exchange risk quality preserves both refreshed and stale equity", arguments: [true, false])
func exchangeRiskDataQualityRoundTrip(_ authoritative: Bool) throws {
    let source = authoritative ? "okx" : "local"
    let error = authoritative ? "null" : "\"account refresh timed out\""
    let snapshot = try decodeRiskQualitySnapshot("""
    {
      "equitySource": "\(source)", "accountRefreshError": \(error),
      "accountRefreshRetryable": \(!authoritative),
      "dailyPnLAuthoritative": \(authoritative)
    }
    """)
    let quality = try #require(snapshot.dataQuality)
    #expect(quality.available == nil)
    #expect(quality.equitySource == source)
    #expect(quality.accountRefreshError == (authoritative ? nil : "account refresh timed out"))
    #expect(quality.accountRefreshRetryable == !authoritative)
    #expect(quality.dailyPnLAuthoritative == authoritative)
    #expect(try roundTripRiskQualitySnapshot(snapshot) == snapshot)
}

@Test("Adding quality metadata does not make existing required risk fields optional")
func riskSnapshotRequiredFieldsRemainRequired() throws {
    let incomplete = """
    {
      "equity": 9400, "equityPeak": 10500, "dayStartEquity": 10000,
      "dailyPnLPercent": -6, "drawdownPercent": 10.47619,
      "killSwitch": true, "strategyCapitals": [], "dataQuality": {}
    }
    """
    do {
        _ = try JSONDecoder().decode(RiskSnapshot.self, from: Data(incomplete.utf8))
        Issue.record("Missing globalNotionals must remain a decoding error")
    } catch DecodingError.keyNotFound(let key, _) {
        #expect(key.stringValue == "globalNotionals")
    }
}
