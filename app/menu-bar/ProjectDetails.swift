import SwiftUI

private let hosts = [
    PickerOption(id: "claude", title: "Claude Code"),
    PickerOption(id: "codex", title: "Codex")
]
private let tiers = [
    PickerOption(id: "gate", title: "Bölüm sonu"),
    PickerOption(id: "step", title: "Her teslim")
]

struct RouteEditor: View {
    @ObservedObject var model: PanelModel
    @State private var host = "claude"
    @State private var tier = "gate"

    var body: some View {
        let activeTier = model.mode == .each ? tier : "gate"
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                labeledPicker("Çalışılan araç", options: hosts, choice: $host, id: "rb.host")
                if model.mode == .each {
                    labeledPicker("Kontrol", options: tiers, choice: $tier, id: "rb.tier")
                } else {
                    Text("Bölüm sonu").font(.callout).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            if let route = model.routes[host]?[activeTier] {
                RouteFields(route: route, modelNames: suggestions, efforts: model.efforts[host] ?? [], busy: model.busy,
                            provider: host == "claude" ? "Codex" : "Claude") { name, effort in
                    model.saveRoute(host: host, tier: activeTier, model: name, effort: effort)
                }
                .id([host, activeTier, route.model, route.effort])
            } else {
                Text("Bu kontrol için model ayarı okunamadı.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private var suggestions: [String] {
        let configured = model.routes[host]?.values.map(\.model).sorted() ?? []
        let examples = host == "claude" ? ["gpt-6-astra", "gpt-6.2-astra", "gpt-6.2-sol"]
                                       : ["fable", "opus", "sonnet"]
        var seen = Set<String>()
        return (configured + examples).filter { seen.insert($0).inserted }
    }

    private func labeledPicker(_ title: String, options: [PickerOption],
                               choice: Binding<String>, id: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            PanelPicker(options: options, selection: choice.wrappedValue, label: title, identifier: id) {
                choice.wrappedValue = $0
            }
            .frame(maxWidth: .infinity).frame(height: 24)
        }
    }
}

private struct RouteFields: View {
    let route: JudgeRoute
    let modelNames: [String]
    let efforts: [String]
    let busy: Bool
    let provider: String
    let save: (String, String) -> Void
    @State private var name: String
    @State private var effort: String

    init(route: JudgeRoute, modelNames: [String], efforts: [String], busy: Bool, provider: String,
         save: @escaping (String, String) -> Void) {
        self.route = route
        self.modelNames = modelNames
        self.efforts = efforts
        self.busy = busy
        self.provider = provider
        self.save = save
        _name = State(initialValue: route.model)
        _effort = State(initialValue: route.effort)
    }

    var body: some View {
        let changed = name != route.model || effort != route.effort
        let valid = name.range(of: #"\A[A-Za-z0-9][A-Za-z0-9._:/-]*\z"#, options: .regularExpression) != nil
        VStack(alignment: .leading, spacing: 7) {
            Text("\(provider) hakem modeli").font(.caption).foregroundStyle(.secondary)
            ModelPicker(name: $name, suggestions: modelNames, label: "\(provider) hakem modeli")
                .frame(maxWidth: .infinity).frame(height: 24)
                .help("Önerilen modellerden birini seçin veya model adını yazın.")
            if changed && !valid {
                Text("Geçerli bir model adı girin.").font(.caption).foregroundStyle(.secondary)
            }
            HStack(alignment: .bottom, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Düşünme düzeyi").font(.caption).foregroundStyle(.secondary)
                    let all = efforts.contains(effort) ? efforts : [effort] + efforts
                    PanelPicker(options: all.map { PickerOption(id: $0, title: $0) },
                                selection: effort, label: "Düşünme düzeyi", identifier: "rb.effort") {
                        effort = $0
                    }
                    .frame(maxWidth: .infinity).frame(height: 24)
                }
                Button("Kaydet") { save(name, effort) }
                    .disabled(!changed || !valid || !efforts.contains(effort) || busy)
                    .accessibilityIdentifier("rb.save")
            }
        }
    }
}

private struct ModelPicker: NSViewRepresentable {
    @Binding var name: String
    let suggestions: [String]
    let label: String

    final class Coordinator: NSObject, NSComboBoxDelegate {
        var name: Binding<String>
        init(name: Binding<String>) { self.name = name }

        func controlTextDidChange(_ notification: Notification) {
            if let box = notification.object as? NSComboBox { name.wrappedValue = box.stringValue }
        }

        func comboBoxSelectionDidChange(_ notification: Notification) {
            if let box = notification.object as? NSComboBox, let model = box.objectValueOfSelectedItem as? String {
                name.wrappedValue = model
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(name: $name) }

    func makeNSView(context: Context) -> NSComboBox {
        let box = NSComboBox(frame: .zero)
        box.delegate = context.coordinator
        box.isEditable = true
        box.completes = false
        box.placeholderString = "Model adı"
        box.setAccessibilityIdentifier("rb.model")
        box.setAccessibilityLabel(label)
        box.setContentHuggingPriority(.defaultLow, for: .horizontal)
        box.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return box
    }

    func updateNSView(_ box: NSComboBox, context: Context) {
        context.coordinator.name = $name
        if box.objectValues.compactMap({ $0 as? String }) != suggestions {
            box.removeAllItems()
            box.addItems(withObjectValues: suggestions)
        }
        if box.stringValue != name { box.stringValue = name }
    }
}

struct SessionList: View {
    @ObservedObject var model: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                if model.sessionsBusy {
                    ProgressView().controlSize(.small).accessibilityLabel("Oturumlar okunuyor")
                } else if model.sessions.isEmpty && model.sessionsMessage == nil {
                    Text("Bu proje için oturum bulunamadı.").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    model.refreshSessions()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(model.sessionsBusy)
                .help("Oturumları yenile")
                .accessibilityLabel("Oturumları yenile")
                .accessibilityIdentifier("rb.sessions.refresh")
            }
            if !model.sessions.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(model.sessions, id: \.self) { session in
                            VStack(alignment: .leading, spacing: 2) {
                                HStack {
                                    Text(session.host == "claude" ? "Claude Code" : "Codex")
                                        .font(.callout.weight(.medium))
                                    Spacer()
                                    Text(session.status == "running" ? "Çalışıyor" : "Kayıtlı")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Text(session.id).font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .accessibilityLabel(session.id)
                            }
                        }
                    }
                    .padding(.trailing, 4)
                }
                .frame(height: min(152, CGFloat(model.sessions.count) * 42))
            }
            if let message = model.sessionsMessage {
                Text(message).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 3)
    }
}
