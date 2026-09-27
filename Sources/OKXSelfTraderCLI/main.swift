import Foundation
import Darwin
import ATKGateway

@main
struct OKXSelfTraderCLI {
    static func main() async {
        let instrumentID = CommandLine.arguments.dropFirst().first ?? "BTC-USDT-SWAP"
        do {
            let client = ATKClient()
            let config = try await client.requireAPIKeyProfile()
            let ticker = try await client.marketTicker(instrumentID: instrumentID)
            let profile = config.defaultProfile ?? "-"
            let site = config.profiles.first(where: { $0.id == profile })?.site ?? "-"
            print("credential=api_key profile=\(profile) site=\(site)")
            print("\(ticker.instrumentID) last=\(ticker.last) bid=\(ticker.bid?.description ?? "-") ask=\(ticker.ask?.description ?? "-")")
        } catch {
            FileHandle.standardError.write(Data("ATK 请求失败：\(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}
