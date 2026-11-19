import AppKit
import ApplicationServices
import Foundation

// Inspect and operate only the application PID supplied by the fixture runner.
struct DriverError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success else { return nil }
    return value
}

func text(_ element: AXUIElement, _ key: String) -> String {
    (attribute(element, key) as? String) ?? ""
}

func describe(_ element: AXUIElement) -> String {
    ["AXRole", "AXIdentifier", "AXTitle", "AXDescription", "AXValue", "AXHelp"].map {
        text(element, $0)
    }.joined(separator: " | ") + " | focused=" + String(describing: attribute(element, "AXFocused") as? Bool)
}

func tree(_ root: AXUIElement) -> [AXUIElement] {
    var pending = [root]
    var seen = Set<AXUIElement>()
    var elements: [AXUIElement] = []
    while let item = pending.popLast() {
        guard seen.insert(item).inserted else { continue }
        elements.append(item)
        for key in ["AXChildren", "AXWindows", "AXExtrasMenuBar", "AXMenuBar", "AXFocusedUIElement"] {
            guard let value = attribute(item, key) else { continue }
            if CFGetTypeID(value) == AXUIElementGetTypeID() {
                pending.append(value as! AXUIElement)
            } else if let children = value as? [AXUIElement] {
                pending.append(contentsOf: children.reversed())
            }
        }
    }
    return elements
}

func wait(_ root: AXUIElement, _ description: String,
          for predicate: ([AXUIElement]) -> Bool) throws {
    let deadline = Date().addingTimeInterval(5)
    repeat {
        if predicate(tree(root)) { return }
        Thread.sleep(forTimeInterval: 0.08)
    } while Date() < deadline
    throw DriverError("Timed out waiting for \(description)\n" + tree(root).map(describe).joined(separator: "\n"))
}

func press(_ element: AXUIElement) throws {
    let status = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard status == .success else { throw DriverError("AXPress \(status.rawValue) on " + describe(element)) }
}

func run() throws {
    let args = Array(CommandLine.arguments.dropFirst())
    guard args.count >= 2, let pid = Int32(args[0]), AXIsProcessTrusted() else {
        throw DriverError("Expected a target pid, command, and Accessibility permission")
    }
    let app = AXUIElementCreateApplication(pid)
    _ = AXUIElementSetMessagingTimeout(app, 1)
    let query = args.count > 2 ? args[2] : ""
    func matches(_ element: AXUIElement) -> Bool {
        if query.hasPrefix("rb.") { return text(element, "AXIdentifier") == query }
        return ["AXTitle", "AXDescription", "AXValue"].contains { text(element, $0) == query }
    }
    switch args[1] {
    case "appearance":
        guard args.count == 4, let image = NSBitmapImageRep(data: try Data(contentsOf: URL(fileURLWithPath: args[3]))),
              let color = image.colorAt(x: image.pixelsWide / 12, y: image.pixelsHigh / 2)?.usingColorSpace(.sRGB) else {
            throw DriverError("Cannot read window capture")
        }
        // Sample the panel's unobstructed leading inset, outside the controls.
        let level = 0.2126 * color.redComponent + 0.7152 * color.greenComponent + 0.0722 * color.blueComponent
        // Window-only captures include material transparency. Compare the
        // neutral label ink with the background, not an absolute background gray.
        var ink: [CGFloat] = []
        for y in stride(from: image.pixelsHigh / 10, to: image.pixelsHigh * 9 / 10, by: 3) {
            for x in stride(from: image.pixelsWide / 9, to: image.pixelsWide * 2 / 3, by: 3) {
                guard let c = image.colorAt(x: x, y: y)?.usingColorSpace(.sRGB),
                      max(c.redComponent, c.greenComponent, c.blueComponent)
                         - min(c.redComponent, c.greenComponent, c.blueComponent) < 0.08 else { continue }
                ink.append(0.2126 * c.redComponent + 0.7152 * c.greenComponent + 0.0722 * c.blueComponent)
            }
        }
        ink.sort()
        guard !ink.isEmpty else { throw DriverError("No neutral panel pixels") }
        let contrast = (ink[ink.count * 99 / 100] - level) - (level - ink[ink.count / 100])
        guard args[2] == "light" ? contrast < -0.05 : contrast > 0.05 else {
            throw DriverError("Expected \(args[2]) panel, sampled sRGB brightness \(level)")
        }
        print(level)
    case "window":
        let entries = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let candidates = entries.filter { window in
            guard window[kCGWindowOwnerPID as String] as? Int32 == pid,
                  let bounds = window[kCGWindowBounds as String] as? [String: NSNumber] else { return false }
            return (bounds["Width"]?.doubleValue ?? 0) > 150 && (bounds["Height"]?.doubleValue ?? 0) > 100
        }
        guard let number = candidates.first?[kCGWindowNumber as String] as? NSNumber else {
            throw DriverError("No visible panel window for fixture process")
        }
        print(number)
    case "dump":
        print(tree(app).map(describe).joined(separator: "\n"))
    case "value", "set-text":
        var target: AXUIElement?
        try wait(app, query) { elements in
            target = elements.first(where: matches)
            return target != nil
        }
        guard let target else { throw DriverError("Missing field " + query) }
        if args[1] == "value" { print(text(target, "AXValue")); return }
        guard args.count == 4 else { throw DriverError("Expected a field and a value") }
        let result = AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        guard result == .success else { throw DriverError("Cannot focus text field " + query) }
        try wait(app, "text editor focus") { elements in
            elements.contains { matches($0) && attribute($0, "AXFocused") as? Bool == true }
        }
        let source = CGEventSource(stateID: .combinedSessionState)
        let units = Array(args[3].utf16)
        for selectAll in [true, false] {
            for down in [true, false] {
                guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else {
                    throw DriverError("Cannot enter text")
                }
                if selectAll { event.flags = .maskCommand }
                else { event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units) }
                event.postToPid(pid)
                Thread.sleep(forTimeInterval: 0.04)
            }
        }
        try wait(app, "edited value") { elements in
            elements.contains { matches($0) && text($0, "AXValue") == args[3] }
        }
    case "menu-closed":
        try wait(app, "menu dismissal") { elements in
            !elements.contains { text($0, "AXRole") == "AXMenuItem" }
        }
    case "selected":
        try wait(app, "confirmed selection " + query) { elements in
            elements.contains { matches($0) && text($0, "AXValue") == "Seçili" && attribute($0, "AXEnabled") as? Bool != false }
        }
    case "wait", "absent":
        try wait(app, query) { elements in elements.contains(where: matches) == (args[1] == "wait") }
    case "press", "option", "focus":
        var target: AXUIElement?
        try wait(app, query) { elements in
            target = elements.first { item in
                matches(item) && (attribute(item, "AXEnabled") as? Bool != false)
                    && (args[1] != "option" || text(item, "AXRole") == "AXMenuItem")
            }
            return target != nil
        }
        guard let target else { throw DriverError("Missing target") }
        if args[1] == "focus" {
            let error = AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            guard error == .success else { throw DriverError("Cannot focus \(query): \(error)") }
            try wait(app, "keyboard focus on " + query) { elements in
                elements.contains { matches($0) && attribute($0, "AXFocused") as? Bool == true }
            }
        } else {
            try press(target)
        }
    case "key", "menu-key":
        guard let code = UInt16(query) else { throw DriverError("Expected a virtual key code") }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: CGEventSource(stateID: .combinedSessionState),
                                      virtualKey: code, keyDown: down) else {
                throw DriverError("Cannot build a key event")
            }
            if args[1] == "menu-key" {
                // AppKit tracks open native menus in the WindowServer event stream.
                guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
                      (!down || tree(app).contains(where: { text($0, "AXRole") == "AXMenuItem" })) else {
                    throw DriverError("The fixture does not own the frontmost open menu")
                }
                event.post(tap: .cghidEventTap)
            } else {
                event.postToPid(pid)
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
    default:
        throw DriverError("Unknown command")
    }
}

do { try run() } catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
