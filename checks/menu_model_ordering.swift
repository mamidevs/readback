// Compile alongside the production PanelModel.swift. Hold one read until another project is selected.
import Foundation

@main
struct MenuOrderingCheck {
    @MainActor static func wait(for condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(6)
        while !condition() {
            if Date() > deadline { throw CocoaError(.fileReadUnknown) }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }

    @MainActor static func main() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: folder) }
        let script = folder.appendingPathComponent("bridge.py")
        try #"""
        import json, pathlib, sys, time
        base = pathlib.Path(__file__).parent
        if sys.argv[1] == 'projects':
            print(json.dumps({'projects': [dict(id=n, name=n, location=n) for n in ('A','B')]}))
        else:
            project = sys.argv[2]
            (base / (project + '.started')).touch()
            for _ in range(500):
                if (base / (project + '.release')).exists():
                    break
                time.sleep(.01)
            else:
                sys.exit(1)
            print(json.dumps({'mode': 'off' if project == 'A' else 'each'}))
        """#.write(to: script, atomically: true, encoding: .utf8)

        let suiteName = "readback-ordering-" + UUID().uuidString
        let preferences = UserDefaults(suiteName: suiteName)!
        defer { preferences.removePersistentDomain(forName: suiteName) }
        let model = PanelModel(bridge: SettingsBridge(script: script), preferences: preferences)
        model.refresh()
        try await wait { model.projects.count == 2 }
        model.select("A")
        try await wait { FileManager.default.fileExists(atPath: folder.appendingPathComponent("A.started").path) }
        model.select("B")
        var observed: [JudgeMode] = []
        let subscription = model.$mode.compactMap { $0 }.sink { observed.append($0) }
        defer { subscription.cancel() }
        try Data().write(to: folder.appendingPathComponent("A.release"))
        try await wait { FileManager.default.fileExists(atPath: folder.appendingPathComponent("B.started").path) }
        try Data().write(to: folder.appendingPathComponent("B.release"))
        try await wait { !model.busy }
        precondition(model.selection == "B" && model.mode == .each && observed == [.each])
        print("A delayed response never marked the newly selected project with the old mode.")
    }
}
