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
    var prominent = false
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
        let button: NSPopUpButton = prominent ? ProjectButton(frame: .zero, pullsDown: false)
                                              : NSPopUpButton(frame: .zero, pullsDown: false)
        if prominent {
            button.isBordered = false
            button.focusRingType = .exterior
        }
        button.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
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

/// The visible project name remains a real pop-up control, including its keyboard actions.
private final class ProjectButton: NSPopUpButton {
    override func draw(_ dirtyRect: NSRect) {
        let font = NSFont.systemFont(ofSize: 20, weight: .semibold)
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = .byTruncatingTail
        let color: NSColor = isEnabled ? .labelColor : .secondaryLabelColor
        let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color, .paragraphStyle: style]
        let name = titleOfSelectedItem ?? "Proje seç"
        let natural = (name as NSString).size(withAttributes: attributes)
        let width = min(natural.width.rounded(.up), max(0, bounds.width - 24))
        let rect = NSRect(x: 0, y: floor((bounds.height - natural.height) / 2), width: width, height: natural.height)
        (name as NSString).draw(in: rect, withAttributes: attributes)
        let configuration = NSImage.SymbolConfiguration(pointSize: 10, weight: .semibold)
            .applying(.init(hierarchicalColor: .secondaryLabelColor))
        let image = NSImage(systemSymbolName: "chevron.down", accessibilityDescription: nil)?
            .withSymbolConfiguration(configuration)
        if let image {
            let size = image.size
            image.draw(in: NSRect(x: width + 7, y: bounds.midY - size.height / 2, width: size.width, height: size.height),
                       from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
        }
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
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 13) {
                Image(systemName: "book.closed.fill")
                    .font(.system(size: 22, weight: .medium))
                    .foregroundStyle(Color(nsColor: PanelStyle.onPine))
                    .frame(width: 42, height: 42)
                    .background(PanelStyle.accent, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Proje").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    PanelPicker(options: projectOptions, selection: model.selection, label: "Proje",
                                identifier: "rb.project", placeholder: "Proje seç", prominent: true, select: model.select)
                        .frame(maxWidth: .infinity).frame(height: 26)
                }
            }
            .padding(.bottom, 18)

            HStack {
                Text("Hakem denetimi").font(.system(size: 13, weight: .semibold))
                Spacer()
                if model.busy {
                    ProgressView().controlSize(.mini).accessibilityLabel("Ayarlar güncelleniyor")
                }
            }
            .padding(.bottom, 7)
            HStack(spacing: 2) {
                ForEach(JudgeMode.allCases, id: \.self) { mode in
                    modeButton(mode)
                }
            }
            .padding(3)
            .background(PanelStyle.inset, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(PanelStyle.line, lineWidth: 0.7))

            if model.selection != nil {
                Rectangle().fill(PanelStyle.line).frame(height: 1).padding(.top, 16).padding(.bottom, 4)
                if let mode = model.mode, mode != .off {
                    sectionButton("Hakem modelleri", icon: "slider.horizontal.3", kind: .routes, id: "rb.routes")
                    if section == .routes {
                        RouteEditor(model: model)
                            .id(model.selection)
                            .padding(12)
                            .background(PanelStyle.inset, in: RoundedRectangle(cornerRadius: 10))
                            .padding(.bottom, 4)
                    }
                }
                sectionButton("Proje oturumları", icon: "rectangle.stack", kind: .sessions, id: "rb.sessions")
                if section == .sessions {
                    SessionList(model: model)
                        .padding(12)
                        .background(PanelStyle.inset, in: RoundedRectangle(cornerRadius: 10))
                }
            }
            if let message = model.message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 8)
                    .accessibilityIdentifier("rb.message")
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 18)
        .padding(.bottom, model.selection == nil ? 18 : 10)
        .frame(width: 348, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
        .background(PanelStyle.canvas)
        .tint(PanelStyle.accent)
    }

    private func sectionButton(_ title: String, icon: String, kind: PanelSection, id: String) -> some View {
        let expanded = section == kind
        return Button {
            section = expanded ? nil : kind
            model.watchSessions(section == .sessions)
        } label: {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .medium))
                    .frame(width: 18)
                    .foregroundStyle(expanded ? PanelStyle.accent : .secondary)
                Text(title).font(.system(size: 13, weight: .medium))
                Spacer()
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 3)
            .frame(height: 34)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
        .frame(height: 29)
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
        button.confirmed = confirmed
        button.isEnabled = enabled
        button.choose = choose
        button.needsDisplay = true
    }
}

/// A native control owns each mode segment, including keyboard focus and activation.
private final class ModeRowButton: NSButton {
    var confirmed = false
    var choose: (() -> Void)?

    @objc func chooseMode() { choose?() }

    override var acceptsFirstResponder: Bool { isEnabled }
    override var focusRingMaskBounds: NSRect { bounds }
    override func drawFocusRingMask() { outline.fill() }

    private var outline: NSBezierPath {
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 6, yRadius: 6)
    }

    override func draw(_ dirtyRect: NSRect) {
        if confirmed || isHighlighted {
            (confirmed ? PanelStyle.pine : NSColor.quaternaryLabelColor).setFill()
            outline.fill()
        }
        let color: NSColor = confirmed ? PanelStyle.onPine : (isEnabled ? .labelColor : .disabledControlTextColor)
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12, weight: confirmed ? .semibold : .regular), .foregroundColor: color
        ]
        let text = title as NSString
        let size = text.size(withAttributes: attributes)
        let extra: CGFloat = confirmed ? 14 : 0
        let x = floor((bounds.width - size.width - extra) / 2)
        if confirmed {
            let configuration = NSImage.SymbolConfiguration(pointSize: 10, weight: .bold)
                .applying(.init(hierarchicalColor: color))
            if let image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: nil)?
                .withSymbolConfiguration(configuration) {
                let iconSize = image.size
                image.draw(in: NSRect(x: x, y: bounds.midY - iconSize.height / 2,
                                      width: iconSize.width, height: iconSize.height),
                           from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
            }
        }
        text.draw(at: NSPoint(x: x + extra, y: floor((bounds.height - size.height) / 2)), withAttributes: attributes)
    }
}
