import SwiftUI
import TradingDomain

struct AccountPanel: View {
    @ObservedObject var model: DashboardModel

    private var primaryAssets: [AccountAsset] {
        model.accountOverview.assets
            .sorted { left, right in
                (valuation(for: left) ?? 0) > (valuation(for: right) ?? 0)
            }
            .prefix(4)
            .map { $0 }
    }

    /// OKX does not always include `eqUsd` for demo balances. Keep the
    /// overview useful by valuing those balances against the latest contract
    /// ticker already loaded by the market sidebar. Stablecoins are valued at
    /// one USD and remain available even while the contract list is warming.
    private func valuation(for asset: AccountAsset) -> Decimal? {
        if let usdValue = asset.usdValue, usdValue >= 0 { return usdValue }
        let currency = asset.currency.uppercased()
        let unitPrice: Double
        if ["USD", "USDT", "USDC", "DAI"].contains(currency) {
            unitPrice = 1
        } else if let contract = model.contracts.first(where: { $0.shortName.uppercased() == currency }) {
            unitPrice = contract.price
        } else {
            return nil
        }
        guard unitPrice.isFinite, unitPrice > 0 else { return nil }
        let quantity = asset.equity.doubleValue
        guard quantity.isFinite, quantity >= 0 else { return nil }
        return Decimal(quantity * unitPrice)
    }

    var body: some View {
        VStack(spacing: 0) {
            Divider().overlay(Color.white.opacity(0.08))
            HStack(spacing: 0) {
                accountMetric("估计总资产", value: model.accountOverview.totalAssetValueUSD.map(formatUSD) ?? "--")
                    .frame(width: 126, alignment: .leading)
                metricDivider
                accountMetric("今日收益", value: model.accountOverview.todayPnLUSD.map(formatSignedUSD) ?? "--", color: pnlColor)
                    .frame(width: 112, alignment: .leading)
                metricDivider
                HStack(spacing: 12) {
                    if primaryAssets.isEmpty {
                        Text("暂无资产明细")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(primaryAssets, content: assetMetric)
                    }
                }
                .layoutPriority(1)
                Spacer(minLength: 10)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            Divider().overlay(Color.white.opacity(0.08))
        }
    }

    private var metricDivider: some View {
        Divider()
            .frame(height: 30)
            .overlay(Color.white.opacity(0.1))
            .padding(.horizontal, 14)
    }

    private var pnlColor: Color {
        (model.accountOverview.todayPnLUSD ?? 0) >= 0 ? .green : .red
    }

    private func accountMetric(_ title: String, value: String, color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).foregroundStyle(color)
        }
        .lineLimit(1)
    }

    private func assetMetric(_ asset: AccountAsset) -> some View {
        let usdValue = valuation(for: asset)
        return VStack(alignment: .leading, spacing: 4) {
            Text(asset.currency)
                .font(.caption.weight(.bold).monospaced())
            Text(usdValue.map(formatUSD) ?? "估值待同步")
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundStyle(usdValue == nil ? Color.secondary : Color.mint)
        }
        .frame(minWidth: 82, alignment: .leading)
        .lineLimit(1)
    }
}
