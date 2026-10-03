import Foundation
import TradingDomain

public enum OKXGatewayError: LocalizedError, Sendable, Equatable {
    case invalidResponse
    case api(code: String, message: String)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "OKX 返回了无法识别的响应"
        case let .api(code, message): return "OKX 错误 \(code)：\(message)"
        }
    }
}

/// The OKX public business WebSocket is the sole source of live candles.
/// Historical backfill and every authenticated call go through the ATK CLI.
public struct OKXCandleSocket: Sendable {
    public let timeoutInterval: TimeInterval

    public init(timeoutInterval: TimeInterval = 15) {
        self.timeoutInterval = timeoutInterval.isFinite && timeoutInterval > 0 ? timeoutInterval : 15
    }

    /// Reconnects the OKX business stream and reports transport state separately
    /// from candle data. No timer or REST request manufactures live candles.
    public func candleEvents(instrumentID: String, interval: KlineInterval) -> AsyncStream<OKXCandleEvent> {
        AsyncStream { continuation in
            let task = Task {
                var retryDelay = 0.5
                var attempt = 0
                let url = URL(string: "wss://ws.okx.com:8443/ws/v5/business")!
                while !Task.isCancelled {
                    attempt += 1
                    continuation.yield(.connecting(attempt: attempt))
                    do { try await OKXConnectionPacer.shared.waitForSlot() }
                    catch { break }
                    var request = URLRequest(url: url)
                    request.timeoutInterval = timeoutInterval
                    let socket = URLSession.shared.webSocketTask(with: request)
                    let health = OKXCandleConnectionHealth()
                    socket.resume()
                    do {
                        try await withTaskCancellationHandler {
                            // OKX uses text ping/pong, not WebSocket control ping
                            // frames. This watchdog also bounds a stalled handshake.
                            let heartbeat = Task {
                                while !Task.isCancelled {
                                    do { try await Task.sleep(for: .seconds(2)) }
                                    catch { return }
                                    switch await health.action() {
                                    case .none: break
                                    case .ping:
                                        do { try await socket.send(.string("ping")) }
                                        catch {
                                            await health.fail("OKX WSS 心跳发送失败：\(error.localizedDescription)")
                                            socket.cancel(with: .goingAway, reason: nil)
                                            return
                                        }
                                    case .disconnect:
                                        socket.cancel(with: .goingAway, reason: nil)
                                        return
                                    }
                                }
                            }
                            defer {
                                heartbeat.cancel()
                                socket.cancel(with: .normalClosure, reason: nil)
                            }
                            let subscription: [String: Any] = ["op": "subscribe", "args": [["channel": "candle\(interval.exchangeBar)", "instId": instrumentID]]]
                            let data = try JSONSerialization.data(withJSONObject: subscription)
                            try await socket.send(.string(String(decoding: data, as: UTF8.self)))
                            while !Task.isCancelled {
                                let message = try await socket.receive()
                                let text: String
                                switch message {
                                case let .string(value): text = value
                                case let .data(value): text = String(decoding: value, as: UTF8.self)
                                @unknown default: continue
                                }
                                let frame = try Self.candleFrame(text, instrumentID: instrumentID, interval: interval)
                                switch frame {
                                case .ignored: continue
                                case .pong:
                                    await health.received(subscribed: false)
                                case .subscribed:
                                    await health.received(subscribed: true)
                                    continuation.yield(.subscribed)
                                case let .candles(candles):
                                    await health.received(subscribed: true)
                                    // A successful send or subscription acknowledgement
                                    // alone does not prove that candles are flowing.
                                    retryDelay = 0.5
                                    attempt = 0
                                    for candle in candles { continuation.yield(.candle(candle)) }
                                }
                            }
                        } onCancel: {
                            socket.cancel(with: .normalClosure, reason: nil)
                        }
                    } catch {
                        socket.cancel(with: .goingAway, reason: nil)
                        guard !Task.isCancelled else { break }
                        let reason = await health.failureReason ?? error.localizedDescription
                        continuation.yield(.reconnecting(reason: reason, retryInSeconds: retryDelay))
                        do { try await Task.sleep(for: .seconds(retryDelay)) }
                        catch { break }
                        retryDelay = min(retryDelay * 2, 15)
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static func candleFrame(_ text: String, instrumentID: String, interval: KlineInterval) throws -> OKXCandleFrame {
        if text == "pong" { return .pong }
        guard let data = text.data(using: .utf8),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw OKXGatewayError.invalidResponse
        }
        if object["event"] as? String == "error" {
            throw OKXGatewayError.api(code: object["code"] as? String ?? "unknown", message: object["msg"] as? String ?? "WSS 订阅失败")
        }
        guard let argument = object["arg"] as? [String: Any],
              argument["instId"] as? String == instrumentID,
              argument["channel"] as? String == "candle\(interval.exchangeBar)" else { return .ignored }
        if object["event"] as? String == "subscribe" { return .subscribed }
        guard let rows = object["data"] as? [[String]], !rows.isEmpty else { return .ignored }
        // WSS rows include the confirmation flag. Treat a truncated or
        // malformed frame as invalid instead of accidentally marking an
        // unfinished bar closed or trading at a truncated price.
        let candles = rows.compactMap(decodeCandle)
        guard candles.count == rows.count else { throw OKXGatewayError.invalidResponse }
        return .candles(candles.sorted { $0.timestamp < $1.timestamp })
    }

    private static func decodeCandle(_ row: [String]) -> Candle? {
        guard row.count >= 9,
              let timestamp = Double(row[0]), timestamp.isFinite, timestamp > 0,
              let open = decimal(row[1]), let high = decimal(row[2]),
              let low = decimal(row[3]), let close = decimal(row[4]),
              let volume = decimal(row[5]),
              let quoteVolume = decimal(row[7]),
              open > 0, high > 0, low > 0, close > 0, volume >= 0,
              quoteVolume >= 0,
              high >= low, high >= open, high >= close, low <= open, low <= close else { return nil }
        guard row[8] == "0" || row[8] == "1" else { return nil }
        let confirmed = row[8] == "1"
        return Candle(timestamp: Date(timeIntervalSince1970: timestamp / 1000), open: open, high: high, low: low, close: close, volume: volume, quoteVolume: quoteVolume, confirmed: confirmed)
    }

    private static func decimal(_ value: String) -> Decimal? {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalized.isEmpty,
              normalized.range(of: #"^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"#, options: .regularExpression) != nil,
              let parsed = Decimal(string: normalized, locale: Locale(identifier: "en_US_POSIX")),
              parsed.isFinite else { return nil }
        return parsed
    }
}
