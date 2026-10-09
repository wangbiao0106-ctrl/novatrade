import Foundation
import Testing
import TradingDomain

// Captured from PaperTradingAccount after a 20-contract market buy with
// ctVal=0.1, last=100 and ask=101. Python-only accounting metadata must not
// prevent the desktop client from reading the shared account/ledger fields.
private let paperAccountPayload = """
{
  "account": {
    "mode": "paper", "executionMode": "paper", "profile": "local-paper",
    "site": "local", "label": "纸面交易", "authenticated": true,
    "initialBalanceUSD": 5000.0, "equityUSD": 4997.899,
    "availableEquityUSD": 4797.899, "totalAssetValueUSD": 4997.899,
    "todayPnLUSD": -0.101, "todayLossCount": 0, "todayAIOrderCount": 1,
    "realizedPnLUSD": -0.10099999999965803,
    "assets": [{"id": "USDT", "currency": "USDT", "equity": 4997.899,
                "available": 4797.899, "usdValue": 4997.899}],
    "positions": [{
      "id": "ca1e7756-13ee-4554-bd72-89c48a519ea1",
      "instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 20.0,
      "entryPrice": 101.0, "markPrice": 100.0, "unrealizedPnL": -2.0,
      "marginMode": "isolated", "margin": 202.0, "leverage": 1.0,
      "takeProfitPrice": null, "stopLossPrice": null, "takeProfitLevels": [],
      "strategyID": "codex", "entryOrderID": "4cf42390-08f7-4a60-91c6-c4af7aa4eb2f"
    }],
    "positionsKnown": true, "pendingOrders": [], "pendingOrdersKnown": true,
    "dataQuality": {"dailyBillsAvailable": true, "dailyBillsPaginationComplete": true,
                    "positionsAvailable": true, "pendingOrdersAvailable": true},
    "updatedAt": "2026-10-08T00:00:00Z"
  },
  "orders": [{
    "id": "4cf42390-08f7-4a60-91c6-c4af7aa4eb2f",
    "strategyID": "71d7727a-24d2-55e2-8b0f-561b332562be",
    "instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 20.0,
    "requestedAt": "2026-10-08T00:00:00Z", "fillPrice": 101.0,
    "status": "filled", "remoteOrderID": null, "clientOrderID": "aipaperclient",
    "signal": null
  }],
  "fills": [{
    "id": "ccaaf41a-ef04-4898-b086-a0f347c81e42",
    "orderID": "4cf42390-08f7-4a60-91c6-c4af7aa4eb2f",
    "price": 101.0, "quantity": 20.0, "fee": 0.101,
    "timestamp": "2026-10-08T00:00:00Z", "instrumentID": "BTC-USDT-SWAP",
    "side": "buy", "reason": null
  }]
}
"""

@Test("The desktop client decodes a real local paper account and execution ledger")
func paperAccountExecutionPayloadDecodes() throws {
    struct Payload: Decodable {
        let account: AccountOverview
        let orders: [PaperOrder]
        let fills: [PaperFill]
    }
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    let payload = try decoder.decode(Payload.self, from: Data(paperAccountPayload.utf8))
    #expect(payload.account.isLocalPaper)
    #expect(payload.account.authenticated)
    #expect(payload.account.usdtEquity == Decimal(string: "4997.899"))
    #expect(payload.account.positions.first?.margin == 202)
    #expect(payload.account.positions.first?.unrealizedPnL == -2)
    let order = try #require(payload.orders.first)
    let fill = try #require(payload.fills.first)
    #expect(order.remoteOrderID == nil)
    #expect(order.status == "filled")
    #expect(order.clientOrderID == "aipaperclient")
    #expect(fill.orderID == order.id)
    #expect(fill.instrumentID == "BTC-USDT-SWAP")
    #expect(fill.side == "buy")
    #expect(fill.reason == nil)
    #expect(fill.price == 101)
    #expect(fill.fee == Decimal(string: "0.101"))
    #expect(fill.timestamp == order.requestedAt)
}
