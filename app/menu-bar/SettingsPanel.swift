import AppKit
import SwiftUI

struct PickerOption: Equatable {
    let id: String
    let title: String
}

struct PanelPicker: NSViewRepresentable {
    let options: [PickerOption]
    let selection: String?
    let label: String
    let identifier: String
    var placeholder: String? = nil
    let select: (String) -> Void

    final class Coordinator: NSObject {
        var select: (String) -> Void
        init(select: @escaping (String) -> Void) { self.select = select }
        @objc func picked(_ sender: NSPopUpButton) {
            if let id = sender.selectedItem?.representedObject as? String { select(id) }
        }
    }
    func makeCoordinator() -> Coordinator { Coordinator(select: select) }

    func makeNSView(context: Context) -> NSPopUpButton {
        let button = NSPopUpButton(frame: .zero, pullsDown: false)
        button.target = context.coordinator
        button.action = #selector(Coordinator.picked(_:))
        button.setAccessibilityLabel(label)
        button.setAccessibilityIdentifier(identifier)
        button.setContentHuggingPriority(.defaultLow, for: .horizontal)
        button.autoenablesItems = false
        return button
    }

    func updateNSView(_ button: NSPopUpButton, context: Context) {
        context.coordinator.select = select
        let ids = button.itemArray.compactMap { $0.representedObject as? String }
        let titles = [placeholder].compactMap { $0 } + options.map(\.title)
        if ids != options.map(\.id) || button.itemTitles != titles {
            button.removeAllItems()
            if let placeholder {
                button.addItem(withTitle: placeholder)
                button.lastItem?.isEnabled = false
            }
            for option in options {
                button.addItem(withTitle: option.title)
                button.lastItem?.representedObject = option.id
            }
        }
        let offset = placeholder == nil ? 0 : 1
        let index = options.firstIndex { $0.id == selection }
        button.selectItem(at: index.map { $0 + offset } ?? (placeholder == nil ? -1 : 0))
        button.isEnabled = !options.isEmpty
    }
}

struct SettingsPanel: View {
    @ObservedObject var model: PanelModel
    @State private var section: PanelSection?

    private enum PanelSection { case routes, sessions }

    private var projectOptions: [PickerOption] {
        let counts = Dictionary(grouping: model.projects, by: \.name)
        return model.projects.map { project in
            PickerOption(id: project.id, title: counts[project.name]?.count == 1
                         ? project.name : "\(project.name) — \(project.location)")
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 7) {
                Text("Proje").font(.headline)
                PanelPicker(options: projectOptions, selection: model.selection, label: "Proje",
                            identifier: "rb.project", placeholder: "Proje seç", select: model.select)
                    .frame(maxWidth: .infinity)
                    .frame(height: 26)
            }

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("Hakem ne zaman devreye girsin?").font(.headline)
                    Spacer()
                    if model.busy {
                        ProgressView().controlSize(.small)
                            .accessibilityLabel("Ayarlar güncelleniyor")
                    }
                }
                ForEach(JudgeMode.allCases, id: \.self) { mode in
                    modeButton(mode)
                }
            }

            if model.selection != nil {
                VStack(alignment: .leading, spacing: 4) {
                    Divider().padding(.bottom, 4)
                    if let mode = model.mode, mode != .off {
                        sectionButton("Hakem modelleri", icon: "slider.horizontal.3", kind: .routes, id: "rb.routes")
                        if section == .routes {
                            RouteEditor(model: model)
                                .id(model.selection)
                                .padding(.vertical, 4)
                        }
                    }
                    sectionButton("Proje oturumları", icon: "rectangle.stack", kind: .sessions, id: "rb.sessions")
                    if section == .sessions {
                        SessionList(model: model)
                    }
                }
            }

            if let message = model.message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("rb.message")
            }
        }
        .padding(18)
        .frame(width: 340, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func sectionButton(_ title: String, icon: String, kind: PanelSection, id: String) -> some View {
        let expanded = section == kind
        return Button {
            section = expanded ? nil : kind
            model.watchSessions(section == .sessions)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: icon).frame(width: 16).foregroundStyle(.secondary)
                Text(title)
                Spacer()
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 3)
            .frame(height: 28)
            .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .accessibilityLabel(title)
        .accessibilityValue(expanded ? "Açık" : "Kapalı")
        .accessibilityIdentifier(id)
    }

    private func modeButton(_ mode: JudgeMode) -> some View {
        ModeButton(mode: mode, confirmed: model.mode == mode,
                   enabled: model.mode != nil && !model.busy) {
            model.setMode(mode)
        }
        .frame(maxWidth: .infinity)
        .frame(height: 32)
    }
}

private struct ModeButton: NSViewRepresentable {
    let mode: JudgeMode
    let confirmed: Bool
    let enabled: Bool
    let choose: () -> Void

    func makeNSView(context: Context) -> ModeRowButton {
        let button = ModeRowButton()
        button.setButtonType(.momentaryPushIn)
        button.isBordered = false
        button.focusRingType = .exterior
        button.target = button
        button.action = #selector(ModeRowButton.chooseMode)
        button.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return button
    }

    func updateNSView(_ button: ModeRowButton, context: Context) {
        button.title = mode.label
        button.setAccessibilityLabel(mode.label)
        button.setAccessibilityIdentifier("rb.mode." + mode.rawValue)
        button.setAccessibilityValue(confirmed ? "Seçili" : "")
        button.symbol = mode.symbol
        button.confirmed = confirmed
        button.isEnabled = enabled
        button.choose = choose
        button.needsDisplay = true
    }
}

/// A single native control owns the entire row, including keyboard focus and activation.
private final class ModeRowButton: NSButton {
    var symbol = ""
    var confirmed = false
    var choose: (() -> Void)?

    @objc func chooseMode() { choose?() }

    override var acceptsFirstResponder: Bool { isEnabled }
    override var focusRingMaskBounds: NSRect { bounds }
    override func drawFocusRingMask() { outline.fill() }

    private var outline: NSBezierPath {
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 7, yRadius: 7)
    }

    override func draw(_ dirtyRect: NSRect) {
        if isHighlighted || confirmed {
            (isHighlighted ? NSColor.quaternaryLabelColor
                           : NSColor.controlAccentColor.withAlphaComponent(0.12)).setFill()
            outline.fill()
        }
        let foreground: NSColor = isEnabled ? .labelColor : .disabledControlTextColor
        let symbolColor: NSColor = isEnabled ? .secondaryLabelColor : .disabledControlTextColor
        drawSymbol(symbol, color: symbolColor, centerX: 19)

        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: NSFont.systemFontSize), .foregroundColor: foreground
        ]
        let text = title as NSString
        let height = text.size(withAttributes: attributes).height
        text.draw(at: NSPoint(x: 38, y: (bounds.height - height) / 2), withAttributes: attributes)
        if confirmed { drawSymbol("checkmark", color: .controlAccentColor, centerX: bounds.maxX - 18) }
    }

    private func drawSymbol(_ name: String, color: NSColor, centerX: CGFloat) {
        let base = NSImage(systemSymbolName: name, accessibilityDescription: nil)
        guard let image = base?.withSymbolConfiguration(.init(hierarchicalColor: color)) else { return }
        let size = image.size
        let rect = NSRect(x: centerX - size.width / 2, y: bounds.midY - size.height / 2,
                          width: size.width, height: size.height)
        image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1,
                   respectFlipped: true, hints: nil)
    }
}
