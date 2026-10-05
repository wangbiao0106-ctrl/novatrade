import SwiftUI

struct AIActivitySheet: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingSettings = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Label("AI 决策日志", systemImage: "list.bullet.rectangle")
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
            AIRuntimeDetailsView(model: model)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .padding(20)
        .frame(width: 920, height: 700, alignment: .topLeading)
        .task { await model.refreshAI() }
        .sheet(isPresented: $showingSettings) {
            AISettingsSheet(model: model)
        }
    }
}
