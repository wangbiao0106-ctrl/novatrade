import SwiftUI
import TradingDomain

struct ChartPanel: View {
    @ObservedObject var model: DashboardModel
    let chartHeight: CGFloat
    @State private var showVolume = true
    @State private var showEMA = true
    @State private var showGrid = true
    @State private var chartResetToken = UUID()
    var body: some View {
        VStack(spacing: 0) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) {
                    intervalControls
                    Spacer(minLength: 0)
                    indicatorControls
                }.fixedSize(horizontal: true, vertical: false)
                VStack(alignment: .leading, spacing: 8) {
                    intervalControls
                    HStack {
                        indicatorControls
                        Spacer(minLength: 0)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12).padding(.vertical, 9)
            Divider().overlay(Color.white.opacity(0.07))
            NativeCandleChart(snapshot: model.marketSnapshot, showVolume: showVolume, showEMA: showEMA, showGrid: showGrid)
                .id(chartResetToken)
                .frame(height: chartHeight)
                .padding(.horizontal, 10)
                .padding(.top, 10)
            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                let stale = model.candleDataSource == .websocket && model.candleLastUpdatedAt.map { timeline.date.timeIntervalSince($0) > 30 } == true
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        candleStatus(stale: stale)
                        Spacer(minLength: 6)
                        Text("拖拽平移 · 双指缩放 · 悬停查看 OHLC").foregroundStyle(.secondary)
                    }
                    candleStatus(stale: stale)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 17)
            .padding(.bottom, 11)
        }.background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.07)))
    }

    private var intervalControls: some View {
        HStack(spacing: 4) {
            Label("K线", systemImage: "chart.bar.xaxis")
                .font(.caption.weight(.semibold)).foregroundStyle(.primary)
                .padding(.trailing, 3)
            Divider().frame(height: 17).padding(.trailing, 3)
            ForEach(KlineInterval.allCases, id: \.self) { interval in
                Button(interval.rawValue) {
                    model.selectInterval(interval)
                    chartResetToken = UUID()
                }
                .buttonStyle(.plain)
                .font(.caption.weight(.medium).monospacedDigit())
                .foregroundStyle(model.selectedInterval == interval ? .white : .secondary)
                .padding(.horizontal, 7).padding(.vertical, 5)
                .background(model.selectedInterval == interval ? Color.white.opacity(0.14) : .clear, in: RoundedRectangle(cornerRadius: 4))
                .fixedSize(horizontal: true, vertical: false)
            }
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private var indicatorControls: some View {
        HStack(spacing: 10) {
            Toggle(isOn: $showEMA) { Label("EMA", systemImage: "chart.line.uptrend.xyaxis") }
                .toggleStyle(.button).font(.caption2).tint(.orange)
            Toggle(isOn: $showVolume) { Label("量", systemImage: "chart.bar.xaxis") }
                .toggleStyle(.button).font(.caption2).tint(.mint)
            Toggle(isOn: $showGrid) { Image(systemName: "square.grid.3x3") }
                .toggleStyle(.button).help("网格")
            Button { chartResetToken = UUID() } label: {
                Label("自动", systemImage: "arrow.up.left.and.arrow.down.right")
            }
            .buttonStyle(.plain).font(.caption2).foregroundStyle(.secondary)
            .help("自动缩放到最新行情")
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func candleStatus(stale: Bool) -> some View {
        HStack(spacing: 8) {
            Label(stale ? "OKX WSS · 行情暂未更新" : model.candleDataSource.label, systemImage: model.candleDataSource.icon)
                .foregroundStyle(model.candleDataSource == .websocket && !stale ? .mint : .orange)
                .help("历史 K 线首次通过 API 加载；实时 K 柱仅由 OKX WSS candle 频道更新")
            if let updated = model.candleLastUpdatedAt {
                Text(updated.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute().second()))
                    .monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
    }
}

struct MarketInsightStrip: View {
    @ObservedObject var model: DashboardModel

    private var candles: [Candle] { Array((model.marketSnapshot?.candles ?? []).suffix(24)) }
    private var latest: Candle? { candles.last }

    var body: some View {
        HStack(spacing: 0) {
            insight("最新收盘", value: latest.map { formatPrice($0.close) } ?? "--", detail: latest?.confirmed == true ? "已收盘" : "当前 K 线")
            insight("区间高低", value: rangeText, detail: "最近 \(candles.count) 根")
            insight("成交量", value: volumeText, detail: "K 线累计")
            insight("K 线状态", value: latest == nil ? "等待数据" : latest?.confirmed == true ? "已确认" : model.candleDataSource == .websocket ? "实时更新" : "等待推送", detail: model.candleDataSource.label)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.07)))
    }

    private var rangeText: String {
        guard let low = candles.map(\.low).min(), let high = candles.map(\.high).max() else { return "--" }
        return "\(formatPrice(low)) – \(formatPrice(high))"
    }

    private var volumeText: String {
        let total = candles.reduce(Decimal.zero) { $0 + $1.volume }
        return formatCompact(total.doubleValue)
    }

    private func insight(_ title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit())
            Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .overlay(alignment: .trailing) { Rectangle().fill(Color.white.opacity(0.08)).frame(width: 1, height: 38) }
    }
}
