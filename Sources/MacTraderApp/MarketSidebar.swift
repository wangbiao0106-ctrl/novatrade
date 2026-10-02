import SwiftUI

struct MarketSidebar: View {
    @ObservedObject var model: DashboardModel
    @State private var searchText = ""
    @State private var debouncedSearchText = ""
    @State private var category: MarketCategory = .mainstream
    @FocusState private var searchFocused: Bool

    private var searchKey: String {
        normalizeSearchText(debouncedSearchText)
    }

    private var isSearching: Bool { !searchKey.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(searchFocused ? .mint : .secondary)
                TextField("搜索合约", text: $searchText)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .onSubmit { searchFocused = false }
                if !searchText.isEmpty { Button { searchText = ""; searchFocused = true } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain).foregroundStyle(.secondary) }
            }
            .padding(9)
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded { searchFocused = true })
            .background(Color.white.opacity(searchFocused ? 0.1 : 0.06), in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(searchFocused ? Color.mint.opacity(0.55) : .clear))
            .padding(.horizontal, 14).padding(.top, 14).padding(.bottom, 12)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    if isSearching {
                        searchResults
                    } else {
                        favoritesSection
                        categoryPicker
                        ForEach(model.contracts(for: category).filter { !model.favoriteIDs.contains($0.id) }) { contract in
                            contractRow(contract)
                        }
                    }
                }
                .padding(.horizontal, 8)
            }
            Spacer(minLength: 12)
        }.background(Color.sidebarBackground).navigationSplitViewColumnWidth(min: 245, ideal: 270, max: 310)
            .task(id: searchText) {
                let value = searchText
                guard !normalizeSearchText(value).isEmpty else {
                    debouncedSearchText = ""
                    return
                }
                do { try await Task.sleep(for: .milliseconds(150)) }
                catch { return }
                guard !Task.isCancelled else { return }
                debouncedSearchText = value
            }
    }

    @ViewBuilder
    private var favoritesSection: some View {
        HStack {
            Text("自选").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            Text("\(model.favoriteContracts.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 5)
        ForEach(model.favoriteContracts) { contract in
            contractRow(contract)
        }
    }

    private var categoryPicker: some View {
        Picker("合约分组", selection: $category) {
            ForEach(MarketCategory.allCases, id: \.self) { Text($0.rawValue).tag($0) }
        }
        .labelsHidden()
        .pickerStyle(.segmented)
        .controlSize(.small)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var searchResults: some View {
        let results = model.contracts.filter { matches($0, query: searchKey) }
        HStack {
            Text("搜索结果").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            Text("\(results.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 5)
        if results.isEmpty {
            Text("未找到匹配合约")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 44)
        } else {
            ForEach(results) { contract in
                contractRow(contract)
            }
        }
    }

    private func contractRow(_ contract: PerpetualContract) -> some View {
        ContractRow(contract: contract, selected: model.selectedContract == contract.id, isFavorite: model.favoriteIDs.contains(contract.id)) {
            model.select(contract)
        } toggleFavorite: {
            model.toggleFavorite(contract)
        }
    }

    private func matches(_ contract: PerpetualContract, query: String) -> Bool {
        [contract.id, contract.name, contract.shortName, contract.pairLabel, contract.category]
            .contains { normalizeSearchText($0).contains(query) }
    }

    private func normalizeSearchText(_ value: String) -> String {
        value.lowercased().filter { $0.isLetter || $0.isNumber }
    }
}

struct ContractRow: View {
    let contract: PerpetualContract
    let selected: Bool
    let isFavorite: Bool
    let action: () -> Void
    let toggleFavorite: () -> Void
    var body: some View {
        HStack(spacing: 6) {
            Button(action: action) {
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 5).fill(contract.accent.opacity(0.2)).frame(width: 24, height: 24).overlay(Text(contract.shortName.prefix(1)).font(.caption.weight(.bold)).foregroundStyle(contract.accent))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(contract.pairLabel)
                            .font(.system(size: 12, weight: .semibold))
                            .minimumScaleFactor(0.8)
                        Text(contract.category + " · 永续").font(.caption2).foregroundStyle(.secondary)
                    }
                    .lineLimit(1)
                    .layoutPriority(1)
                    Spacer(minLength: 2)
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(formatPrice(contract.price))
                            .font(.system(size: 11, design: .monospaced))
                            .minimumScaleFactor(0.8)
                        Text(formatSignedPercent(contract.change))
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(contract.change >= 0 ? Color.marketRise : Color.marketFall)
                    }
                    .lineLimit(1)
                    .layoutPriority(1)
                    .help(formatPrice(contract.price))
                }
            }.buttonStyle(.plain).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: toggleFavorite) { Image(systemName: isFavorite ? "star.fill" : "star").font(.caption).foregroundStyle(isFavorite ? .yellow : .secondary) }.buttonStyle(.plain).help(isFavorite ? "取消收藏" : "收藏合约")
        }
        .padding(.vertical, 9).padding(.horizontal, 9)
        .background(selected ? Color.white.opacity(0.1) : .clear, in: RoundedRectangle(cornerRadius: 7))
        .overlay(alignment: .leading) { if selected { Capsule().fill(.mint).frame(width: 3, height: 26) } }
    }
}
