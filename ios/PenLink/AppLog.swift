import Foundation

/// On-screen log (3-finger tap shows it). Every line is also sent to the PC, so you can read it there.
final class AppLog: ObservableObject {
    static let shared = AppLog()

    @Published private(set) var lines: [String] = []
    /// Set by USBServer: forwards each line to the PC while connected.
    var forward: ((String) -> Void)?

    func add(_ text: String) {
        let stamp = AppLog.stamp()
        DispatchQueue.main.async {
            self.lines.append(stamp + " " + text)
            if self.lines.count > 200 {
                self.lines.removeFirst(self.lines.count - 200)
            }
        }
        forward?(text)
    }

    private static func stamp() -> String {
        let parts = Calendar.current.dateComponents([.hour, .minute, .second], from: Date())
        return String(format: "%02d:%02d:%02d", parts.hour ?? 0, parts.minute ?? 0, parts.second ?? 0)
    }
}
