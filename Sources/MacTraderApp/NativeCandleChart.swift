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
    let trailingWidth: CGFloat

    var count: CGFloat { min(capacity, CGFloat(max(total, 1))) }
    var dataWidth: CGFloat { max(1, width - trailingWidth) }
    // Keep the default view inset from the right edge, while preserving the
    // full canvas spacing when the user pans into history. That lets candles
    // occupy the transparent price-axis area instead of stopping at its edge.
    var step: CGFloat { width / count }
    var trailingBars: CGFloat { trailingWidth / max(step, 1) }
    var maximumOffset: CGFloat {
        guard total > Int(count) else { return 0 }
        return max(0, CGFloat(total) - count + trailingBars)
    }
    var start: CGFloat { maximumOffset - min(maximumOffset, max(0, offset)) }
    var renderStart: CGFloat { min(max(0, start), CGFloat(max(total - 1, 0))) }
    var dataRight: CGFloat {
        let trailingGapIsVisible = total > Int(count) && start + count > CGFloat(total)
        return trailingGapIsVisible ? dataWidth : width
    }
    var indices: Range<Int> {
        let lower = max(0, Int(floor(renderStart)) - 1)
        let upper = min(total, Int(ceil(renderStart + count)) + 1)
        return lower..<max(lower, upper)
    }
    func x(_ index: Int) -> CGFloat { (CGFloat(index) - renderStart + 0.5) * step }
    func index(at x: CGFloat) -> Int {
        min(max(0, total - 1), max(0, Int(floor(renderStart + x / step))))
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
    // Price labels are drawn over the right edge of the plot. Keep the data
    // viewport at the full canvas width so the axis does not steal candle
    // drawing space.
    var plotWidth: CGFloat { max(1, size.width) }
    var trailingWidth: CGFloat { max(axisWidth + 16, min(140, size.width * 0.10)) }
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
    let showMA: Bool
    let showSupportResistance: Bool

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
            let viewport = CandleViewport(total: candles.count, capacity: visibleCount, offset: barOffset,
                                          width: layout.plotWidth, trailingWidth: layout.trailingWidth)
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
                        .padding(.trailing, 12)
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
                        if dragOrigin == nil, location.x >= 0, location.x <= viewport.dataRight,
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
                    barOffset = min(viewport.maximumOffset, barOffset)
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
                    if showMA {
                        let closes = candles.map { $0.close.doubleValue }
                        Text("MA5 \(price(movingAverage(closes, period: 5)[selectedIndex]))").foregroundStyle(.orange)
                        Text("MA10 \(price(movingAverage(closes, period: 10)[selectedIndex]))").foregroundStyle(.pink)
                        Text("MA20 \(price(movingAverage(closes, period: 20)[selectedIndex]))").foregroundStyle(.cyan)
                    }
                    Text("VOL \(formatCompact(candle.volume.doubleValue))").foregroundStyle(.secondary)
                }
                .lineLimit(1).minimumScaleFactor(0.75)
                if showSupportResistance, let levels = supportResistance() {
                    HStack(spacing: 12) {
                        Text("阻力 \(price(levels.resistance))").foregroundStyle(.pink)
                        Text("支撑 \(price(levels.support))").foregroundStyle(.orange)
                    }
                    .lineLimit(1).minimumScaleFactor(0.75)
                }
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
            stroke(&context, from: CGPoint(x: 0, y: y), to: CGPoint(x: plot.maxX, y: y), color: .white.opacity(0.065))
        }
        for index in tickIndices {
            let x = viewport.x(index)
            stroke(&context, from: CGPoint(x: x, y: plot.minY), to: CGPoint(x: x, y: layout.dataBottom), color: .white.opacity(0.045))
            context.draw(Text(axisDate(candles[index].timestamp, viewport: viewport)).font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: x, y: layout.dataBottom + 14))
        }
        stroke(&context, from: CGPoint(x: plot.maxX, y: 0), to: CGPoint(x: plot.maxX, y: layout.size.height), color: .white.opacity(0.10))
        stroke(&context, from: CGPoint(x: 0, y: layout.dataBottom), to: CGPoint(x: plot.maxX, y: layout.dataBottom), color: .white.opacity(0.10))
        // Draw axis tick text beneath market data. The price axis is an
        // overlay without a backing panel, so candles remain visible when
        // the viewport is scrolled beneath the axis labels.
        drawPriceAxis(context: &context, layout: layout, scale: scale, axisFont: axisFont)

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
        if showMA {
            let closes = candles.map { $0.close.doubleValue }
            var maContext = context
            maContext.clip(to: Path(plot))
            for (period, color) in [(5, Color.orange), (10, Color.pink), (20, Color.cyan)] {
                let series = movingAverage(closes, period: period)
                drawSeries(&maContext, series: series, viewport: viewport, plot: plot, scale: scale, color: color, lineWidth: period == 5 ? 1.1 : 1.25)
            }
        }
        if showSupportResistance, let levels = supportResistance() {
            let resistanceY = scale.y(levels.resistance, in: plot)
            let supportY = scale.y(levels.support, in: plot)
            if resistanceY >= plot.minY && resistanceY <= plot.maxY {
                stroke(&context, from: CGPoint(x: 0, y: resistanceY), to: CGPoint(x: plot.maxX, y: resistanceY), color: .pink.opacity(0.9), dash: [5, 4])
            }
            if supportY >= plot.minY && supportY <= plot.maxY {
                stroke(&context, from: CGPoint(x: 0, y: supportY), to: CGPoint(x: plot.maxX, y: supportY), color: .orange.opacity(0.9), dash: [5, 4])
            }
        }
        if showVolume {
            let y = plot.maxY + 9
            stroke(&context, from: CGPoint(x: 0, y: y), to: CGPoint(x: plot.maxX, y: y), color: .white.opacity(0.10))
            context.draw(Text("成交量").font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: 8, y: y + 9), anchor: .leading)
            context.draw(Text(formatCompact(maxVolume)).font(axisFont).foregroundStyle(.secondary), at: CGPoint(x: axisLabelX(layout: layout), y: layout.volumeRect.minY + 7), anchor: .leading)
        }
        drawLatestPrice(context: &context, layout: layout, viewport: viewport, scale: scale)
        drawCrosshair(context: &context, layout: layout, viewport: viewport, scale: scale)
    }

    private func axisLabelX(layout: CandleChartLayout) -> CGFloat {
        max(4, layout.size.width - layout.axisWidth + 6)
    }

    private func drawPriceAxis(context: inout GraphicsContext, layout: CandleChartLayout, scale: CandlePriceScale, axisFont: Font) {
        let plot = layout.priceRect
        for tick in 0...4 {
            let y = plot.minY + plot.height * CGFloat(tick) / 4
            let value = scale.high - scale.range * Double(tick) / 4
            context.draw(Text(price(value)).font(axisFont).foregroundStyle(.secondary),
                         at: CGPoint(x: axisLabelX(layout: layout), y: y), anchor: .leading)
        }
    }

    private func drawLatestPrice(context: inout GraphicsContext, layout: CandleChartLayout, viewport: CandleViewport, scale: CandlePriceScale) {
        guard let latest = candles.last else { return }
        let rawY = scale.y(latest.close.doubleValue, in: layout.priceRect)
        let color = Color.white.opacity(0.82)
        let inRange = rawY >= layout.priceRect.minY && rawY <= layout.priceRect.maxY
        let latestX = viewport.x(candles.count - 1)
        let latestIsOffRight = latestX > viewport.dataWidth
        let currentY = min(layout.priceRect.maxY - 9, max(layout.priceRect.minY + 9, rawY))
        if inRange {
            let startX = latestIsOffRight ? 0 : max(0, latestX)
            stroke(&context, from: CGPoint(x: startX, y: currentY), to: CGPoint(x: layout.plotWidth, y: currentY), color: color, dash: [4, 3])
        }
        let arrow = !inRange ? (rawY < layout.priceRect.minY ? "↑ " : "↓ ") : (latestIsOffRight ? "← " : "")
        let tagX = layout.plotWidth - layout.axisWidth / 2
        axisTag(&context, text: arrow + price(latest.close), center: CGPoint(x: tagX, y: currentY), color: Color.white.opacity(0.12), maxWidth: layout.axisWidth - 2)
    }

    private func drawCrosshair(context: inout GraphicsContext, layout: CandleChartLayout, viewport: CandleViewport, scale: CandlePriceScale) {
        guard let pointer else { return }
        let index = viewport.index(at: pointer.x)
        guard candles.indices.contains(index) else { return }
        let x = viewport.x(index)
        stroke(&context, from: CGPoint(x: x, y: layout.priceRect.minY), to: CGPoint(x: x, y: layout.dataBottom), color: .white.opacity(0.55), dash: [4, 3])
        if layout.priceRect.contains(pointer) {
            stroke(&context, from: CGPoint(x: 0, y: pointer.y), to: CGPoint(x: layout.plotWidth, y: pointer.y), color: .white.opacity(0.45), dash: [4, 3])
            axisTag(&context, text: price(scale.value(at: pointer.y, in: layout.priceRect)), center: CGPoint(x: layout.plotWidth - layout.axisWidth / 2, y: pointer.y), color: Color(white: 0.27), maxWidth: layout.axisWidth - 2)
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
        let maximumTicks = max(2, floor(viewport.dataRight / 112))
        let stride = max(1, Int(ceil(viewport.count / maximumTicks)))
        return viewport.indices.filter { $0 % stride == 0 && viewport.x($0) >= 42 && viewport.x($0) <= viewport.dataRight - 42 }
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
    private func movingAverage(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty else { return [] }
        var result = Array(repeating: 0.0, count: values.count)
        var sum = 0.0
        for index in values.indices {
            sum += values[index]
            if index >= period { sum -= values[index - period] }
            result[index] = sum / Double(min(index + 1, period))
        }
        return result
    }

    private func drawSeries(_ context: inout GraphicsContext, series: [Double], viewport: CandleViewport, plot: CGRect, scale: CandlePriceScale, color: Color, lineWidth: CGFloat) {
        guard series.count == candles.count else { return }
        var path = Path()
        for (offset, index) in viewport.indices.enumerated() {
            let point = CGPoint(x: viewport.x(index), y: scale.y(series[index], in: plot))
            if offset == 0 { path.move(to: point) } else { path.addLine(to: point) }
        }
        context.stroke(path, with: .color(color.opacity(0.95)), lineWidth: lineWidth)
    }

    private func supportResistance() -> (support: Double, resistance: Double)? {
        guard let latest = candles.last else { return nil }
        let lookback: TimeInterval = snapshot?.interval == .oneDay ? 30 * 86_400 : 24 * 3_600
        let start = latest.timestamp.addingTimeInterval(-lookback)
        let recent = candles.filter { $0.timestamp >= start }
        guard recent.count >= 3 else { return nil }

        // Use confirmed local swing points from the selected timeframe rather
        // than the visible viewport's raw extrema. This keeps levels tied to
        // intraday structure while updating as each new bar is confirmed.
        let confirmed = recent.filter(\.confirmed)
        let source = confirmed.count >= 3 ? confirmed : recent
        let radius = source.count >= 5 ? 2 : 1
        var supports: [Double] = []
        var resistances: [Double] = []
        if source.count > radius * 2 {
            for index in radius..<(source.count - radius) {
                let low = source[index].low.doubleValue
                let high = source[index].high.doubleValue
                let lows = source[(index - radius)...(index + radius)].map { $0.low.doubleValue }
                let highs = source[(index - radius)...(index + radius)].map { $0.high.doubleValue }
                if low <= (lows.min() ?? low) { supports.append(low) }
                if high >= (highs.max() ?? high) { resistances.append(high) }
            }
        }
        let price = latest.close.doubleValue
        let fallbackSupport = source.map { $0.low.doubleValue }.min() ?? price
        let fallbackResistance = source.map { $0.high.doubleValue }.max() ?? price
        let support = supports.filter { $0 <= price }.max() ?? fallbackSupport
        let resistance = resistances.filter { $0 >= price }.min() ?? fallbackResistance
        guard resistance > support else { return nil }
        return (support, resistance)
    }
}
