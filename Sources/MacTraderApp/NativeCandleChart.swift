import SwiftUI
import TradingDomain

/// A candle keeps its position in data space as the viewport is moved. The
/// fractional bar offset is intentional: rounding here makes dragging jump.
/// The chart redraws on every pointer move, so its date formatters are built once.
@MainActor
private enum ChartDateFormat {
    static let day = formatter("yy/MM/dd")
    static let dayTime = formatter("MM/dd HH:mm")
    static let time = formatter("HH:mm")
    static let full = formatter("yyyy/MM/dd HH:mm")

    private static func formatter(_ pattern: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = pattern
        // Keep chart labels in the operator's local timezone.  Daily candle
        // boundaries are UTC data semantics and must not leak into display.
        formatter.timeZone = .autoupdatingCurrent
        return formatter
    }
}

private struct CandleViewport {
    let total: Int
    let capacity: CGFloat
    let offset: CGFloat
    let width: CGFloat

    var count: CGFloat { min(capacity, CGFloat(max(total, 1))) }
    var step: CGFloat { width / count }
    var maximumOffset: CGFloat { max(0, CGFloat(total) - count) }
    var start: CGFloat { maximumOffset - min(maximumOffset, max(0, offset)) }
    var indices: Range<Int> {
        max(0, Int(floor(start)) - 1)..<min(total, Int(ceil(start + count)) + 1)
    }
    func x(_ index: Int) -> CGFloat { (CGFloat(index) - start + 0.5) * step }
    func index(at x: CGFloat) -> Int {
        min(max(0, total - 1), max(0, Int(floor(start + x / step))))
    }
}

private struct CandlePriceScale {
    let low: Double
    let high: Double
    var range: Double { high - low }

    init(candles: [Candle], indices: Range<Int>) {
        let values = indices.map { candles[$0] }
        let minimum = values.map(\.low.doubleValue).min() ?? 0
        let maximum = values.map(\.high.doubleValue).max() ?? 1
        // Padding also gives a valid scale for a flat or single-candle market.
        let span = max(maximum - minimum, max(max(abs(maximum), abs(minimum)) * 0.001, 1e-16))
        low = minimum - span * 0.09
        high = maximum + span * 0.09
    }
    func y(_ value: Double, in rect: CGRect) -> CGFloat {
        rect.maxY - CGFloat((value - low) / range) * rect.height
    }
    func value(at y: CGFloat, in rect: CGRect) -> Double {
        low + Double((rect.maxY - y) / rect.height) * range
    }
}

private struct CandleChartLayout {
    let size: CGSize
    let showVolume: Bool
    var axisWidth: CGFloat = 80
    let headerHeight: CGFloat = 54
    let timeAxisHeight: CGFloat = 27
    var plotWidth: CGFloat { max(1, size.width - axisWidth) }
    var volumeHeight: CGFloat { showVolume ? max(42, size.height * 0.15) : 0 }
    var priceRect: CGRect {
        CGRect(x: 0, y: headerHeight, width: plotWidth,
               height: max(24, size.height - headerHeight - timeAxisHeight - volumeHeight - (showVolume ? 23 : 0)))
    }
    var volumeRect: CGRect {
        CGRect(x: 0, y: priceRect.maxY + 23, width: plotWidth, height: volumeHeight)
    }
    var dataBottom: CGFloat { size.height - timeAxisHeight }
}

struct NativeCandleChart: View {
    let snapshot: MarketSnapshot?
    let showVolume: Bool
    let showEMA: Bool
    let showGrid: Bool

    @State private var visibleCount: CGFloat = 90
    @State private var barOffset: CGFloat = 0
    @State private var dragOrigin: CGFloat?
    @State private var dragScale: CandlePriceScale?
    @State private var zoomOrigin: CGFloat?
    @State private var pointer: CGPoint?

    private var candles: [Candle] { snapshot?.candles ?? [] }
    private var identity: String { "\(snapshot?.instrumentID ?? ""):\(snapshot?.interval.rawValue ?? "")" }

    var body: some View {
        GeometryReader { geometry in
            let layout = CandleChartLayout(size: geometry.size, showVolume: showVolume,
                                           axisWidth: candles.last.map { abs($0.close.doubleValue) < 0.001 ? 104 : 80 } ?? 80)
            let viewport = CandleViewport(total: candles.count, capacity: visibleCount, offset: barOffset, width: layout.plotWidth)
            let scale = dragScale ?? CandlePriceScale(candles: candles, indices: viewport.indices)
            if candles.isEmpty {
                ContentUnavailableView("等待 OKX K 线", systemImage: "chart.xyaxis.line", description: Text("正在连接 OKX 行情"))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ZStack(alignment: .topLeading) {
                    Canvas { context, _ in
                        drawChart(context: &context, layout: layout, viewport: viewport, scale: scale)
                    }
                    .allowsHitTesting(false)
                    chartHeader(viewport: viewport)
                        .padding(.horizontal, 10).padding(.top, 6)
                        .allowsHitTesting(false)
                    if barOffset > 0.5 {
                        Button {
                            barOffset = 0
                            pointer = nil
                            dragScale = nil
                        } label: {
                            Label("回到最新", systemImage: "arrow.right.to.line")
                                .font(.caption2).padding(.horizontal, 8).padding(.vertical, 5)
                        }
                        .buttonStyle(.plain)
                        .background(Color(white: 0.15), in: Capsule())
                        .padding(.trailing, layout.axisWidth + 12)
                        .padding(.bottom, layout.timeAxisHeight + 8)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing)
                    }
                }
                .background(Color(red: 0.025, green: 0.032, blue: 0.045))
                .clipShape(RoundedRectangle(cornerRadius: 5))
                .contentShape(Rectangle())
                .onContinuousHover { phase in
                    switch phase {
                    case .active(let location):
                        if dragOrigin == nil, location.x >= 0, location.x <= layout.plotWidth,
                           location.y >= layout.priceRect.minY, location.y <= layout.dataBottom {
                            pointer = location
                        } else { pointer = nil }
                    case .ended: pointer = nil
                    }
                }
                .simultaneousGesture(DragGesture(minimumDistance: 2).onChanged { value in
                    if dragOrigin == nil {
                        dragOrigin = barOffset
                        dragScale = scale
                    }
                    barOffset = min(viewport.maximumOffset, max(0, (dragOrigin ?? 0) + value.translation.width / viewport.step))
                    pointer = nil
                }.onEnded { _ in
                    dragOrigin = nil
                    dragScale = nil
                })
                .simultaneousGesture(MagnifyGesture().onChanged { value in
                    if zoomOrigin == nil { zoomOrigin = visibleCount }
                    visibleCount = min(200, max(24, (zoomOrigin ?? 90) / value.magnification))
                    barOffset = min(max(0, CGFloat(candles.count) - visibleCount), barOffset)
                    pointer = nil
                }.onEnded { _ in zoomOrigin = nil })
                .simultaneousGesture(SpatialTapGesture(count: 2).onEnded { _ in resetViewport() })
            }
        }
        .onChange(of: identity) { _, _ in resetViewport() }
        .onChange(of: snapshot?.candles.last?.timestamp) { old, new in
            guard barOffset > 0, let old, let new, new > old else { return }
            // Keep historical candles anchored when a new live candle arrives.
            let advance = CGFloat(new.timeIntervalSince(old) / intervalSeconds)
            barOffset = min(max(0, CGFloat(candles.count) - visibleCount), barOffset + advance)
        }
    }

    private func resetViewport() {
        visibleCount = 90
        barOffset = 0
        dragOrigin = nil
        dragScale = nil
        zoomOrigin = nil
        pointer = nil
    }

    private var intervalSeconds: TimeInterval {
        switch snapshot?.interval ?? .oneHour {
        case .oneMinute: 60
        case .fiveMinutes: 300
        case .fifteenMinutes: 900
        case .oneHour: 3_600
        case .fourHours: 14_400
        case .oneDay: 86_400
        }
    }

    @ViewBuilder
    private func chartHeader(viewport: CandleViewport) -> some View {
        let selectedIndex = pointer.map { viewport.index(at: $0.x) } ?? max(0, candles.count - 1)
        if candles.indices.contains(selectedIndex) {
            let candle = candles[selectedIndex]
            let color = candle.close >= candle.open ? Color.marketRise : Color.marketFall
            let open = candle.open.doubleValue
            let change = open == 0 ? 0 : (candle.close.doubleValue - open) / open * 100
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 8) {
                    Text(pointer == nil ? "最新" : ChartDateFormat.dayTime.string(from: candle.timestamp))
                        .foregroundStyle(.secondary)
                    Text("开 \(price(candle.open))")
                    Text("高 \(price(candle.high))")
                    Text("低 \(price(candle.low))")
                    Text("收 \(price(candle.close))")
                    Text(String(format: "%+.2f%%", change))
                }
                .foregroundStyle(color)
                .lineLimit(1).minimumScaleFactor(0.75)
                HStack(spacing: 12) {
                    if showEMA {
                        let closes = candles.map { $0.close.doubleValue }
                        Text("EMA20 \(price(ema(closes, period: 20)[selectedIndex]))").foregroundStyle(.orange)
                        Text("EMA60 \(price(ema(closes, period: 60)[selectedIndex]))").foregroundStyle(.blue)
                    }
                    Text("VOL \(formatCompact(candle.volume.doubleValue))").foregroundStyle(.secondary)
                }
                .lineLimit(1).minimumScaleFactor(0.75)
            }
            .font(.system(size: 10, design: .monospaced))
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func drawChart(context: inout GraphicsContext, layout: CandleChartLayout, viewport: CandleViewport, scale: CandlePriceScale) {
        let plot = layout.priceRect
        let tickIndices = timeTicks(viewport: viewport)
        let axisFont = Font.system(size: 10, design: .monospaced)
        for tick in 0...4 {
            let y = plot.minY + plot.height * CGFloat(tick) / 4
            let value = scale.high - scale.range * Double(tick) / 4
            if showGrid { stroke(&context, from: CGPoint(x: 0, y: y), to: CGPoint(x: plot.maxX, y: y), color: .white.opacity(0.065)) }
            context.draw(Text(price(value)).font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: plot.maxX + 8, y: y), anchor: .leading)
        }
        for index in tickIndices {
            let x = viewport.x(index)
            if showGrid { stroke(&context, from: CGPoint(x: x, y: plot.minY), to: CGPoint(x: x, y: layout.dataBottom), color: .white.opacity(0.045)) }
            context.draw(Text(axisDate(candles[index].timestamp, viewport: viewport)).font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: x, y: layout.dataBottom + 14))
        }
        stroke(&context, from: CGPoint(x: plot.maxX, y: 0), to: CGPoint(x: plot.maxX, y: layout.size.height), color: .white.opacity(0.10))
        stroke(&context, from: CGPoint(x: 0, y: layout.dataBottom), to: CGPoint(x: plot.maxX, y: layout.dataBottom), color: .white.opacity(0.10))

        var drawing = context
        drawing.clip(to: Path(plot))
        var volumeDrawing = context
        volumeDrawing.clip(to: Path(layout.volumeRect))
        let maxVolume = max(1, viewport.indices.map { candles[$0].volume.doubleValue }.max() ?? 1)
        for index in viewport.indices {
            let candle = candles[index]
            let x = viewport.x(index)
            let color = candle.close >= candle.open ? Color.marketRise : Color.marketFall
            let bodyWidth = max(1, min(16, viewport.step * 0.68))
            let openY = scale.y(candle.open.doubleValue, in: plot)
            let closeY = scale.y(candle.close.doubleValue, in: plot)
            stroke(&drawing, from: CGPoint(x: x, y: scale.y(candle.high.doubleValue, in: plot)), to: CGPoint(x: x, y: scale.y(candle.low.doubleValue, in: plot)), color: color)
            drawing.fill(Path(CGRect(x: x - bodyWidth / 2, y: min(openY, closeY), width: bodyWidth, height: max(1, abs(openY - closeY)))), with: .color(color))
            if showVolume {
                let height = CGFloat(candle.volume.doubleValue / maxVolume) * layout.volumeRect.height
                volumeDrawing.fill(Path(CGRect(x: x - bodyWidth / 2, y: layout.volumeRect.maxY - height, width: bodyWidth, height: height)), with: .color(color.opacity(0.42)))
            }
        }
        if showEMA {
            let closes = candles.map { $0.close.doubleValue }
            var emaContext = context
            emaContext.clip(to: Path(plot))
            for (period, color) in [(20, Color.orange), (60, Color.blue)] {
                let series = ema(closes, period: period)
                var path = Path()
                for (offset, index) in viewport.indices.enumerated() {
                    let point = CGPoint(x: viewport.x(index), y: scale.y(series[index], in: plot))
                    if offset == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
                emaContext.stroke(path, with: .color(color.opacity(0.95)), lineWidth: 1.25)
            }
        }
        if showVolume {
            let y = plot.maxY + 9
            stroke(&context, from: CGPoint(x: 0, y: y), to: CGPoint(x: plot.maxX, y: y), color: .white.opacity(0.10))
            context.draw(Text("成交量").font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: 8, y: y + 9), anchor: .leading)
            context.draw(Text(formatCompact(maxVolume)).font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: plot.maxX + 8, y: layout.volumeRect.minY + 7), anchor: .leading)
        }
        drawLatestPrice(context: &context, layout: layout, scale: scale)
        drawCrosshair(context: &context, layout: layout, viewport: viewport, scale: scale)
    }

    private func drawLatestPrice(context: inout GraphicsContext, layout: CandleChartLayout, scale: CandlePriceScale) {
        guard let latest = candles.last else { return }
        let rawY = scale.y(latest.close.doubleValue, in: layout.priceRect)
        let color = latest.close >= latest.open ? Color.marketRise : Color.marketFall
        let inRange = rawY >= layout.priceRect.minY && rawY <= layout.priceRect.maxY
        if inRange {
            stroke(&context, from: CGPoint(x: 0, y: rawY), to: CGPoint(x: layout.plotWidth, y: rawY), color: color.opacity(0.7), dash: [4, 3])
        }
        let y = min(layout.priceRect.maxY - 9, max(layout.priceRect.minY + 9, rawY))
        let arrow = inRange ? "" : (rawY < layout.priceRect.minY ? "↑ " : "↓ ")
        axisTag(&context, text: arrow + price(latest.close), center: CGPoint(x: layout.plotWidth + layout.axisWidth / 2, y: y), color: color, maxWidth: layout.axisWidth - 2)
    }

    private func drawCrosshair(context: inout GraphicsContext, layout: CandleChartLayout, viewport: CandleViewport, scale: CandlePriceScale) {
        guard let pointer else { return }
        let index = viewport.index(at: pointer.x)
        guard candles.indices.contains(index) else { return }
        let x = viewport.x(index)
        stroke(&context, from: CGPoint(x: x, y: layout.priceRect.minY), to: CGPoint(x: x, y: layout.dataBottom), color: .white.opacity(0.55), dash: [4, 3])
        if layout.priceRect.contains(pointer) {
            stroke(&context, from: CGPoint(x: 0, y: pointer.y), to: CGPoint(x: layout.plotWidth, y: pointer.y), color: .white.opacity(0.45), dash: [4, 3])
            axisTag(&context, text: price(scale.value(at: pointer.y, in: layout.priceRect)), center: CGPoint(x: layout.plotWidth + layout.axisWidth / 2, y: pointer.y), color: Color(white: 0.27), maxWidth: layout.axisWidth - 2)
        }
        axisTag(&context, text: ChartDateFormat.full.string(from: candles[index].timestamp), center: CGPoint(x: min(layout.plotWidth - 65, max(65, x)), y: layout.dataBottom + 14), color: Color(white: 0.22), maxWidth: 150)
    }

    private func axisTag(_ context: inout GraphicsContext, text: String, center: CGPoint, color: Color, maxWidth: CGFloat) {
        let label = Text(text).font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundStyle(.white)
        let resolved = context.resolve(label)
        let measured = resolved.measure(in: CGSize(width: maxWidth - 6, height: 20))
        let width = min(maxWidth, measured.width + 8)
        let rect = CGRect(x: center.x - width / 2, y: center.y - 9, width: width, height: 18)
        context.fill(Path(roundedRect: rect, cornerRadius: 2), with: .color(color))
        context.draw(resolved, at: center)
    }

    private func stroke(_ context: inout GraphicsContext, from: CGPoint, to: CGPoint, color: Color, dash: [CGFloat] = []) {
        var path = Path(); path.move(to: from); path.addLine(to: to)
        context.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: 1, dash: dash))
    }

    private func timeTicks(viewport: CandleViewport) -> [Int] {
        let maximumTicks = max(2, floor(viewport.width / 112))
        let stride = max(1, Int(ceil(viewport.count / maximumTicks)))
        return viewport.indices.filter { $0 % stride == 0 && viewport.x($0) >= 42 && viewport.x($0) <= viewport.width - 42 }
    }

    private func axisDate(_ date: Date, viewport: CandleViewport) -> String {
        if snapshot?.interval == .oneDay { return ChartDateFormat.day.string(from: date) }
        if intervalSeconds * Double(viewport.count) >= 86_400 { return ChartDateFormat.dayTime.string(from: date) }
        return ChartDateFormat.time.string(from: date)
    }

    private func price(_ value: Decimal) -> String { price(value.doubleValue) }
    private func price(_ value: Double) -> String {
        if abs(value) >= 1_000 { return String(format: "%.1f", value) }
        if abs(value) >= 1 { return String(format: "%.2f", value) }
        if abs(value) >= 0.001 { return String(format: "%.5f", value) }
        guard value != 0 else { return "0" }
        let decimals = min(16, max(8, Int(ceil(-log10(abs(value)))) + 3))
        return String(format: "%.*f", decimals, value)
    }
    private func ema(_ values: [Double], period: Int) -> [Double] {
        guard let first = values.first else { return [] }
        let alpha = 2.0 / Double(period + 1)
        var previous = first
        return values.map { value in
            previous += alpha * (value - previous)
            return previous
        }
    }
}
