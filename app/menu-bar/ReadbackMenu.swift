import AppKit
import SwiftUI

private final class PanelController: NSHostingController<SettingsPanel> {
    var dismiss: (() -> Void)?
    override func cancelOperation(_ sender: Any?) { dismiss?() }
}

@MainActor
final class MenuDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var item: NSStatusItem!
    private let panel = NSPopover()
    private let model = PanelModel(bridge: SettingsBridge(
        script: Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/python/menu_settings.py")
    ))

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = item.button else { return }
        let image = NSImage(systemSymbolName: "book.closed", accessibilityDescription: "Readback")
        image?.isTemplate = true
        button.image = image
        button.toolTip = "Readback"
        button.setAccessibilityLabel("Readback")
        button.setAccessibilityIdentifier("rb.status")
        button.target = self
        button.action = #selector(toggle)
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])

        let controller = PanelController(rootView: SettingsPanel(model: model))
        controller.sizingOptions = [.preferredContentSize]
        controller.dismiss = { [weak self] in self?.panel.performClose(nil) }
        panel.delegate = self
        panel.behavior = .transient
        panel.contentViewController = controller
    }

    func popoverWillClose(_ notification: Notification) {
        model.closeActivity()
    }

    @objc private func toggle() {
        guard let button = item.button else { return }
        if NSApp.currentEvent?.type == .rightMouseUp {
            panel.performClose(nil)
            let menu = NSMenu()
            menu.addItem(withTitle: "Readback’ten çık", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item.menu = menu
            button.performClick(nil)
            item.menu = nil
        } else if panel.isShown {
            panel.performClose(nil)
        } else {
            model.refresh()
            // A status-bar button can have a different appearance from the user's app windows.
            let scheme = NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua])
            panel.appearance = scheme.flatMap(NSAppearance.init(named:))
            NSApp.activate(ignoringOtherApps: true)
            panel.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            panel.contentViewController?.view.window?.makeKey()
        }
    }
}

@main
struct ReadbackMenu {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = MenuDelegate()
        application.setActivationPolicy(.accessory)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
