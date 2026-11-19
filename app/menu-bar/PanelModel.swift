import Combine
import Foundation

struct Project: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let location: String
}

enum JudgeMode: String, Codable, CaseIterable {
    case off, gate, each

    var label: String {
        switch self {
        case .off: "Kapalı"
        case .gate: "Bölüm sonunda"
        case .each: "Her teslimde"
        }
    }

    var symbol: String {
        switch self {
        case .off: "minus.circle"
        case .gate: "flag"
        case .each: "checklist"
        }
    }
}

struct BridgeResponse: Decodable {
    var projects: [Project]?
    var mode: JudgeMode?
}

struct SettingsBridge {
    let script: URL

    func run(_ arguments: [String]) throws -> BridgeResponse {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-E", "-s", "-B", script.path] + arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw CocoaError(.fileReadUnknown) }
        return try JSONDecoder().decode(BridgeResponse.self, from: output)
    }
}

@MainActor
final class PanelModel: ObservableObject {
    @Published private(set) var projects: [Project] = []
    @Published private(set) var selection: String?
    @Published private(set) var mode: JudgeMode?
    @Published private(set) var busy = false
    @Published private(set) var message: String?

    private let preferences: UserDefaults
    private let bridge: SettingsBridge
    private let queue = DispatchQueue(label: "readback.settings.requests")
    private var revision = 0
    private let lastProjectKey = "lastSelectedProjectID"

    init(bridge: SettingsBridge, preferences: UserDefaults = .standard) {
        self.bridge = bridge
        self.preferences = preferences
    }

    private func request(_ arguments: [String], failure: String,
                         apply: @escaping @MainActor (BridgeResponse) throws -> Void) {
        revision += 1
        let version = revision
        busy = true
        message = nil
        // Enqueue immediately: a subsequent refresh must observe a previously submitted save.
        queue.async { [bridge] in
            let result = Result { try bridge.run(arguments) }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.revision == version else { return }
                self.busy = false
                do { try apply(result.get()) }
                catch { self.message = failure }
            }
        }
    }

    func refresh() {
        selection = nil
        mode = nil
        projects = []
        request(["projects"], failure: "Projeler okunamadı. Paneli yeniden açıp deneyin.") { response in
            guard let list = response.projects else { throw CocoaError(.coderReadCorrupt) }
            self.projects = list
            let last = self.preferences.string(forKey: self.lastProjectKey)
            if let project = list.first(where: { $0.id == last }) ?? (list.count == 1 ? list.first : nil) {
                self.select(project.id)
            } else if list.isEmpty {
                self.message = "Readback projesi bulunamadı."
            }
        }
    }

    func select(_ id: String) {
        guard projects.contains(where: { $0.id == id }) else { return }
        selection = id
        mode = nil
        preferences.set(id, forKey: lastProjectKey)
        request(["get", id], failure: "Hakem ayarı okunamadı. Projeyi yeniden seçin.") { response in
            guard let saved = response.mode else { throw CocoaError(.coderReadCorrupt) }
            self.mode = saved
        }
    }

    func setMode(_ value: JudgeMode) {
        guard let id = selection, let saved = mode, saved != value, !busy else { return }
        request(["set", id, value.rawValue], failure: "Hakem ayarı kaydedilemedi. Yeniden deneyin.") { response in
            guard response.mode == value else { throw CocoaError(.coderReadCorrupt) }
            self.mode = value
        }
    }
}
