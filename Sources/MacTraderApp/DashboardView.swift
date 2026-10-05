import SwiftUI

struct DashboardView: View {
    @StateObject private var model = DashboardModel()
    @State private var showingNewStrategy = false

    var body: some View {
        NavigationSplitView {
            MarketSidebar(model: model)
        } detail: {
            VStack(spacing: 0) {
                DashboardWindowHeader(model: model)
                AccountPanel(model: model)
                HStack(alignment: .top, spacing: 0) {
                    GeometryReader { geometry in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 12) {
                                MarketHeader(model: model)
                                ChartPanel(model: model, chartHeight: max(320, geometry.size.height - 96))
                            }.padding(16)
                        }
                    }
                    Divider().overlay(Color.white.opacity(0.07))
                    RightRail(model: model, showingNewStrategy: $showingNewStrategy)
                }
            }
            .background(Color.appBackground)
            .ignoresSafeArea(.container, edges: .top)
        }
        .sheet(isPresented: $showingNewStrategy) { NewStrategySheet(model: model) }
        // The creation sheet reports its own failures; everything else that
        // sets `errorMessage` surfaces here.
        .alert("操作失败", isPresented: Binding(
            get: { model.errorMessage != nil && !showingNewStrategy },
            set: { if !$0 { model.errorMessage = nil } }
        )) {
            Button("确定", role: .cancel) { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "")
        }
        .task { await model.refresh() }
        .preferredColorScheme(.dark)
        .frame(minWidth: 1180, minHeight: 760)
    }
}
