import Foundation
import Testing
import TradingDomain

@Test
func legacyStrategySignalWithoutHLSRExitFieldsStillDecodes() throws {
    let strategyID = UUID()
    let current = StrategySignal(strategyID: strategyID, type: "entry_short", price: 100,
                                  reason: "legacy", timestamp: Date(timeIntervalSince1970: 1_700_000_000),
                                  stopPrice: 105, takePrice: 95)
    let encoded = try JSONEncoder().encode(current)
    var object = try #require(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
    object.removeValue(forKey: "moveStopToEntryAfterTP1")
    object.removeValue(forKey: "takePrices")
    object.removeValue(forKey: "targetFractions")
    object.removeValue(forKey: "trailBars")
    object.removeValue(forKey: "invalidationPrice")
    let legacy = try JSONSerialization.data(withJSONObject: object)

    let decoded = try JSONDecoder().decode(StrategySignal.self, from: legacy)
    #expect(decoded.strategyID == strategyID)
    #expect(decoded.moveStopToEntryAfterTP1 == false)
    #expect(decoded.takePrices == nil)
    #expect(decoded.targetFractions == nil)
    #expect(decoded.trailBars == nil)
    #expect(decoded.invalidationPrice == nil)
}

@Test
func candleLegacyPayloadWithoutQuoteVolumeKeepsQuoteVolumeNil() throws {
    let payload = #"{"timestamp":1700000000,"open":100,"high":101,"low":99,"close":100,"volume":12,"confirmed":true}"#.data(using: .utf8)!
    let candle = try JSONDecoder().decode(Candle.self, from: payload)
    #expect(candle.quoteVolume == nil)
    #expect(candle.volume == 12)
}
