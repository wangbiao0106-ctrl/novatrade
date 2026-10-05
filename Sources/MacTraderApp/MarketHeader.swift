import SwiftUI
import TradingDomain

struct MarketHeader: View {
    @ObservedObject var model: DashboardModel

    private var loadedCandles: [Candle] { model.marketSnapshot?.candles ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 24) {
                    contractIdentity
                    Spacer(minLength: 8)
                    priceSummary
                }
                .fixedSize(horizontal: true, vertical: false)
                VStack(alignment: .leading, spacing: 7) {
                    contractIdentity
                    priceSummary
                }
            }
            HStack(spacing: 18) {
                rangeMetric("区间最高", value: loadedCandles.map(\.high).max().map(formatPrice) ?? "--")
                rangeMetric("区间最低", value: loadedCandles.map(\.low).min().map(formatPrice) ?? "--")
                rangeMetric("24h 成交量", value: model.selected.map { formatCompact($0.volume24h) } ?? "--")
                Spacer(minLength: 0)
            }
            .help("区间高低取当前周期已加载的 \(loadedCandles.count) 根真实 K 线")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var contractIdentity: some View {
        Group {
            if let selected = model.selected {
                HStack(spacing: 8) {
                    ContractIcon(symbol: selected.shortName, size: 30)
                    Text(selected.pairLabel).font(.title3.weight(.bold))
                    Text("永续").font(.caption2.weight(.semibold)).foregroundStyle(.mint)
                        .padding(.horizontal, 6).padding(.vertical, 3).background(.mint.opacity(0.12), in: Capsule())
                }
            } else {
                Label("等待实时合约数据", systemImage: "clock.arrow.circlepath")
                    .font(.title3.weight(.semibold)).foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private var priceSummary: some View {
        let change = model.currentChange ?? 0
        return HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(model.currentPrice.map(formatPrice) ?? "--")
                .font(.system(size: 27, weight: .bold, design: .rounded)).monospacedDigit()
                .foregroundStyle(model.currentChange == nil ? .secondary : (change >= 0 ? Color.marketRise : Color.marketFall))
            Text(model.currentChange.map(formatSignedPercent) ?? "--")
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundStyle(model.currentChange == nil ? .secondary : (change >= 0 ? Color.marketRise : Color.marketFall))
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func rangeMetric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit())
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }
}
