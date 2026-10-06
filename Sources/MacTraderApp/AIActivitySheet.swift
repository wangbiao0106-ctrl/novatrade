import SwiftUI
import TradingDomain

struct AIActivitySheet: View {
    @ObservedObject var model: DashboardModel
    let strategy: AIStrategyID
    @Environment(\.dismiss) private var dismiss
    @State private var showingSettings = false

    init(model: DashboardModel, strategy: AIStrategyID = .codex) {
        self.model = model
        self.strategy = strategy
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Label("\(strategy.displayName) 决策日志", systemImage: "list.bullet.rectangle")
                    .font(.title3.weight(.semibold))
                Spacer()
                Button { showingSettings = true } label: {
                    Label("参数设置", systemImage: "slider.horizontal.3")
                }
                .buttonStyle(.borderless)
                Button("完成") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            Divider()
            AIRuntimeDetailsView(model: model, strategy: strategy)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .padding(20)
        .frame(width: 920, height: 700, alignment: .topLeading)
        .task { await model.refreshAI() }
        .sheet(isPresented: $showingSettings) {
            AISettingsSheet(model: model, strategy: strategy)
        }
    }
}
