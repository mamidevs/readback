import AppKit
import SwiftUI

struct ProjectPicker: NSViewRepresentable {
    let projects: [Project]
    let selection: String?
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
        button.setAccessibilityLabel("Proje")
        button.setAccessibilityIdentifier("rb.project")
        button.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return button
    }

    func updateNSView(_ button: NSPopUpButton, context: Context) {
        context.coordinator.select = select
        let ids = button.itemArray.compactMap { $0.representedObject as? String }
        let counts = Dictionary(grouping: projects, by: \.name)
        let titles = projects.map { project in
            counts[project.name]?.count == 1 ? project.name : "\(project.name) — \(project.location)"
        }
        if button.numberOfItems == 0 || ids != projects.map(\.id) || titles != Array(button.itemTitles.dropFirst()) {
            button.removeAllItems()
            button.addItem(withTitle: "Proje seç")
            button.item(at: 0)?.isEnabled = false
            button.autoenablesItems = false
            for (project, title) in zip(projects, titles) {
                button.addItem(withTitle: title)
                button.lastItem?.representedObject = project.id
            }
        }
        let index = projects.firstIndex { $0.id == selection }
        button.selectItem(at: index.map { $0 + 1 } ?? 0)
        button.isEnabled = !projects.isEmpty
    }
}

struct SettingsPanel: View {
    @ObservedObject var model: PanelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 7) {
                Text("Proje").font(.headline)
                ProjectPicker(projects: model.projects, selection: model.selection, select: model.select)
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

            if let message = model.message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("rb.message")
            }
        }
        .padding(18)
        .frame(width: 300, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
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
