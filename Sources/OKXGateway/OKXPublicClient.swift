import Foundation
import TradingDomain

public enum OKXGatewayError: LocalizedError, Sendable {
    case invalidResponse
    case api(code: String, message: String)
    case invalidNumber(String)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "OKX 返回了无法识别的响应"
        case let .api(code, message): return "OKX 错误 \(code)：\(message)"
        case let .invalidNumber(value): return "无法解析数值：\(value)"
        }
    }
}

public protocol HTTPSession: Sendable {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}

extension URLSession: HTTPSession {}

public struct OKXPublicClient: Sendable {
    public let baseURL: URL
    private let session: any HTTPSession
    private let decoder: JSONDecoder

    public init(baseURL: URL = URL(string: "https://www.okx.com")!, session: any HTTPSession = URLSession.shared) {
        self.baseURL = baseURL
        self.session = session
        self.decoder = JSONDecoder()
    }

    public func ticker(instrumentID: String) async throws -> MarketTicker {
        var components = URLComponents(url: baseURL.appendingPathComponent("api/v5/market/ticker"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "instId", value: instrumentID)]
        guard let url = components?.url else { throw OKXGatewayError.invalidResponse }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw OKXGatewayError.invalidResponse
        }
        let envelope = try decoder.decode(OKXEnvelope<[OKXTicker]>.self, from: data)
        guard envelope.code == "0", let item = envelope.data.first else {
            throw OKXGatewayError.api(code: envelope.code, message: envelope.msg)
        }
        guard let last = Decimal(string: item.last, locale: Locale(identifier: "en_US_POSIX")) else {
            throw OKXGatewayError.invalidNumber(item.last)
        }
        return MarketTicker(
            instrumentID: item.instID,
            last: last,
            bid: item.bid.flatMap { Decimal(string: $0, locale: Locale(identifier: "en_US_POSIX")) },
            ask: item.ask.flatMap { Decimal(string: $0, locale: Locale(identifier: "en_US_POSIX")) },
            timestamp: Self.dateFromMilliseconds(item.timestamp) ?? .now
        )
    }

    public func candles(instrumentID: String, interval: KlineInterval, limit: Int = 200) async throws -> [Candle] {
        var components = URLComponents(url: baseURL.appendingPathComponent("api/v5/market/candles"), resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "instId", value: instrumentID),
            URLQueryItem(name: "bar", value: interval.okxBar),
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 300)))
        ]
        let envelope: OKXEnvelope<[[String]]> = try await get(components)
        return envelope.data.compactMap(Self.decodeCandle).sorted { $0.timestamp < $1.timestamp }
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
                    let socket = URLSession.shared.webSocketTask(with: url)
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
                            let subscription: [String: Any] = ["op": "subscribe", "args": [["channel": "candle\(interval.okxBar)", "instId": instrumentID]]]
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

    /// Compatibility adapter for consumers interested only in candle data.
    public func candleUpdates(instrumentID: String, interval: KlineInterval) -> AsyncStream<Candle> {
        let events = candleEvents(instrumentID: instrumentID, interval: interval)
        return AsyncStream { continuation in
            let task = Task {
                for await event in events {
                    if case let .candle(candle) = event { continuation.yield(candle) }
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
              argument["channel"] as? String == "candle\(interval.okxBar)" else { return .ignored }
        if object["event"] as? String == "subscribe" { return .subscribed }
        guard let rows = object["data"] as? [[String]] else { return .ignored }
        guard !rows.isEmpty else { return .ignored }
        // WSS rows include the confirmation flag. Treat a truncated frame as
        // invalid instead of accidentally marking an unfinished bar closed.
        let candles = rows.compactMap { row in row.count >= 9 ? decodeCandle(row) : nil }
        guard candles.count == rows.count else { throw OKXGatewayError.invalidResponse }
        return .candles(candles.sorted { $0.timestamp < $1.timestamp })
    }

    public func orderBook(instrumentID: String, depth: Int = 20) async throws -> OrderBookSnapshot {
        var components = URLComponents(url: baseURL.appendingPathComponent("api/v5/market/books"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "instId", value: instrumentID), URLQueryItem(name: "sz", value: String(min(max(depth, 1), 400)))]
        let envelope: OKXEnvelope<[OKXOrderBook]> = try await get(components)
        let book = envelope.data.first
        let timestamp = Self.dateFromMilliseconds(book?.timestamp) ?? .now
        return OrderBookSnapshot(instrumentID: instrumentID, bids: book?.bids.compactMap(Self.decimalRow) ?? [], asks: book?.asks.compactMap(Self.decimalRow) ?? [], timestamp: timestamp)
    }

    public func trades(instrumentID: String, limit: Int = 50) async throws -> [TradeTick] {
        var components = URLComponents(url: baseURL.appendingPathComponent("api/v5/market/trades"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "instId", value: instrumentID), URLQueryItem(name: "limit", value: String(min(max(limit, 1), 500)))]
        let envelope: OKXEnvelope<[OKXTrade]> = try await get(components)
        return envelope.data.compactMap { trade in
            guard let price = Decimal(string: trade.price, locale: Locale(identifier: "en_US_POSIX")), let size = Decimal(string: trade.size, locale: Locale(identifier: "en_US_POSIX")) else { return nil }
            let timestamp = Self.dateFromMilliseconds(trade.timestamp) ?? .now
            return TradeTick(id: trade.tradeID, instrumentID: instrumentID, price: price, size: size, side: trade.side, timestamp: timestamp)
        }
    }

    private func get<T: Decodable>(_ components: URLComponents?) async throws -> OKXEnvelope<T> {
        guard let url = components?.url else { throw OKXGatewayError.invalidResponse }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw OKXGatewayError.invalidResponse }
        let envelope = try decoder.decode(OKXEnvelope<T>.self, from: data)
        guard envelope.code == "0" else { throw OKXGatewayError.api(code: envelope.code, message: envelope.msg) }
        return envelope
    }

    private static func decimalRow(_ row: [String]) -> [Decimal]? {
        let values = row.compactMap { Decimal(string: $0, locale: Locale(identifier: "en_US_POSIX")) }
        return values.count == row.count ? values : nil
    }

    private static func decodeCandle(_ row: [String]) -> Candle? {
        guard row.count >= 6,
              let timestamp = Double(row[0]), timestamp.isFinite, timestamp > 0,
              let open = Decimal(string: row[1], locale: Locale(identifier: "en_US_POSIX")),
              let high = Decimal(string: row[2], locale: Locale(identifier: "en_US_POSIX")),
              let low = Decimal(string: row[3], locale: Locale(identifier: "en_US_POSIX")),
              let close = Decimal(string: row[4], locale: Locale(identifier: "en_US_POSIX")),
              let volume = Decimal(string: row[5], locale: Locale(identifier: "en_US_POSIX")) else { return nil }
        return Candle(timestamp: Date(timeIntervalSince1970: timestamp / 1000), open: open, high: high, low: low, close: close, volume: volume, confirmed: row.count < 9 || row[8] == "1")
    }

    private static func dateFromMilliseconds(_ value: String?) -> Date? {
        guard let value, let milliseconds = Double(value), milliseconds.isFinite, milliseconds > 0 else { return nil }
        return Date(timeIntervalSince1970: milliseconds / 1000)
    }
}

private struct OKXEnvelope<T: Decodable>: Decodable {
    let code: String
    let msg: String
    let data: T
}

private struct OKXTicker: Decodable {
    let instID: String
    let last: String
    let bid: String?
    let ask: String?
    let timestamp: String?

    enum CodingKeys: String, CodingKey {
        case instID = "instId"
        case last
        case bid = "bidPx"
        case ask = "askPx"
        case timestamp = "ts"
    }
}

private struct OKXOrderBook: Decodable {
    let asks: [[String]]
    let bids: [[String]]
    let timestamp: String

    enum CodingKeys: String, CodingKey { case asks, bids, timestamp = "ts" }
}

private struct OKXTrade: Decodable {
    let tradeID: String
    let price: String
    let size: String
    let side: String?
    let timestamp: String

    enum CodingKeys: String, CodingKey { case tradeID = "tradeId", price = "px", size = "sz", side, timestamp = "ts" }
}
