import AppKit
import SwiftUI

enum PanelStyle {
    private static func pair(_ light: UInt32, _ dark: UInt32) -> NSColor {
        NSColor(name: nil) { appearance in
            let rgb = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((rgb >> 16) & 255) / 255,
                           green: CGFloat((rgb >> 8) & 255) / 255,
                           blue: CGFloat(rgb & 255) / 255, alpha: 1)
        }
    }

    static let canvas = Color(nsColor: pair(0xFAFAF7, 0x202522))
    static let inset = Color(nsColor: pair(0xF0F2EC, 0x292F2B))
    static let line = Color(nsColor: pair(0xDDE1D8, 0x424A44))
    static let pine = pair(0x295B49, 0xB5DCC8)
    static let onPine = pair(0xFFFFFF, 0x173729)
    static let accent = Color(nsColor: pine)
}
