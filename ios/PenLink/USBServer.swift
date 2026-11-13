import Foundation
import Network
import UIKit

/// Listens on a TCP port. The PC reaches it through the USB cable (usbmuxd), then sends
/// screen frames and receives Apple Pencil samples. One PC connection at a time; a newer one replaces the old.
final class USBServer: ObservableObject {
    static let port: UInt16 = 8765

    @Published var status = "Starting..."
    @Published var connected = false

    /// Called on the main thread with each decoded JPEG frame.
    var onFrame: ((UIImage) -> Void)?
    /// Called on the main thread when a PC connects: picture size in pixels and the codec ("h264" or "jpeg").
    var onHello: ((CGSize, String) -> Void)?
    /// Shows H.264 frames. PenView adds its layer to the screen.
    let player = H264Player()

    private let queue = DispatchQueue(label: "penlink.net")
    private let decodeQueue = DispatchQueue(label: "penlink.decode", qos: .userInteractive)
    private var listener: NWListener?
    private var connection: NWConnection?
    private var ready = false
    private var parser = FrameParser()
    private var retry: DispatchWorkItem?

    // only touched on decodeQueue
    private var frameCount = 0
    private var statsStart = Date()

    init() {
        AppLog.shared.forward = { [weak self] text in
            self?.send(.log, Data(text.utf8))
        }
    }

    // MARK: start / stop (call from any thread)

    func start() {
        queue.async { self.startListener() }
    }

    func stop() {
        queue.async { self.tearDown() }
    }

    // MARK: listener

    private func startListener() {
        if listener != nil { return }
        retry?.cancel()
        guard let port = NWEndpoint.Port(rawValue: USBServer.port) else { return }

        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true

        do {
            let l = try NWListener(using: params, on: port)
            l.stateUpdateHandler = { [weak self] state in
                self?.listenerChanged(state)
            }
            l.newConnectionHandler = { [weak self] c in
                self?.accept(c)
            }
            listener = l
            l.start(queue: queue)
        } catch {
            AppLog.shared.add("Cannot listen: \(error)")
            setStatus("Cannot listen on port \(USBServer.port)")
            scheduleRetry()
        }
    }

    private func listenerChanged(_ state: NWListener.State) {
        switch state {
        case .ready:
            AppLog.shared.add("Listening on port \(USBServer.port)")
            if !ready { setStatus("Waiting for the PC (USB)") }
        case .failed(let error):
            AppLog.shared.add("Listener failed: \(error)")
            listener?.cancel()
            listener = nil
            scheduleRetry()
        default:
            break
        }
    }

    private func scheduleRetry() {
        retry?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.startListener()
        }
        retry = work
        queue.asyncAfter(deadline: .now() + 1.0, execute: work)
    }

    private func tearDown() {
        retry?.cancel()
        connection?.cancel()
        connection = nil
        listener?.cancel()
        listener = nil
        ready = false
        setConnected(false)
        setStatus("Paused")
    }

    // MARK: connection

    private func accept(_ c: NWConnection) {
        connection?.cancel()
        parser = FrameParser()
        ready = false
        connection = c
        AppLog.shared.add("Incoming connection from \(c.endpoint)")
        c.stateUpdateHandler = { [weak self, weak c] state in
            guard let self = self, let c = c else { return }
            self.connectionChanged(c, state)
        }
        c.start(queue: queue)
    }

    private func connectionChanged(_ c: NWConnection, _ state: NWConnection.State) {
        switch state {
        case .ready:
            if connection === c {
                ready = true
                setConnected(true)
                setStatus("PC connected, waiting for the picture")
                AppLog.shared.add("PC connected")
                receive(c)
            }
        case .failed, .cancelled:
            if connection === c {
                connection = nil
                ready = false
                setConnected(false)
                setStatus("Waiting for the PC (USB)")
                AppLog.shared.add("PC disconnected")
            }
        default:
            break
        }
    }

    private func receive(_ c: NWConnection) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 1024 * 1024) { [weak self, weak c] data, _, isComplete, error in
            guard let self = self, let c = c, self.connection === c else { return }
            if let data = data, !data.isEmpty {
                self.parser.feed(data)
                while let message = self.parser.next() {
                    self.handle(message.type, message.payload)
                }
                if self.parser.broken {
                    AppLog.shared.add("Protocol error, dropping the connection")
                    c.cancel()
                    return
                }
            }
            if error != nil || isComplete {
                c.cancel()
                return
            }
            self.receive(c)
        }
    }

    private func handle(_ type: UInt8, _ payload: Data) {
        guard let kind = MsgType(rawValue: type) else { return }
        switch kind {
        case .hello:
            guard let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else { return }
            let width = (obj["fw"] as? NSNumber)?.doubleValue ?? 0
            let height = (obj["fh"] as? NSNumber)?.doubleValue ?? 0
            let codec = obj["codec"] as? String ?? "jpeg"
            AppLog.shared.add("Hello: \(Int(width)) x \(Int(height)), \(codec)")
            decodeQueue.async { [weak self] in
                self?.player.reset()
            }
            DispatchQueue.main.async { [weak self] in
                self?.onHello?(CGSize(width: width, height: height), codec)
            }
        case .vconfig:
            guard let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
                  let spsText = obj["sps"] as? String, let ppsText = obj["pps"] as? String,
                  let sps = Data(base64Encoded: spsText), let pps = Data(base64Encoded: ppsText) else {
                AppLog.shared.add("Bad video config")
                return
            }
            let nal = (obj["nal"] as? NSNumber)?.intValue ?? 4
            decodeQueue.async { [weak self] in
                self?.player.configure(sps: sps, pps: pps, nalLength: nal)
            }
        case .frame:
            decodeQueue.async { [weak self] in
                self?.decode(payload)
            }
        case .video:
            if payload.count < 2 { return }
            let keyframe = (payload[0] & 1) != 0
            let body = payload.subdata(in: 1..<payload.count)
            decodeQueue.async { [weak self] in
                guard let self = self else { return }
                self.player.enqueue(body, keyframe: keyframe)
                self.countFrame()
            }
        default:
            break
        }
    }

    private func decode(_ payload: Data) {
        let raw = UIImage(data: payload)
        let image = raw?.preparingForDisplay() ?? raw
        if let image = image {
            DispatchQueue.main.async { [weak self] in
                self?.onFrame?(image)
            }
        } else {
            AppLog.shared.add("Could not decode a frame (\(payload.count) bytes)")
        }
        send(.ack)
        countFrame()
    }

    /// Counts frames and logs the rate every 5 seconds. Only called on decodeQueue.
    private func countFrame() {
        frameCount += 1
        if frameCount == 1 { setStatus("Connected") }
        let elapsed = Date().timeIntervalSince(statsStart)
        if elapsed >= 5 {
            AppLog.shared.add(String(format: "%.0f fps", Double(frameCount) / elapsed))
            frameCount = 0
            statsStart = Date()
        }
    }

    // MARK: sending (any thread)

    func send(_ type: MsgType, _ payload: Data = Data()) {
        queue.async {
            guard self.ready, let c = self.connection else { return }
            c.send(content: Wire.pack(type, payload), completion: .contentProcessed { _ in })
        }
    }

    func sendPen(_ samples: [PenSample]) {
        if samples.isEmpty { return }
        let body = "[" + samples.map { $0.json }.joined(separator: ",") + "]"
        send(.pen, Data(body.utf8))
    }

    // MARK: published state (always updated on the main thread)

    private func setStatus(_ text: String) {
        DispatchQueue.main.async { self.status = text }
    }

    private func setConnected(_ value: Bool) {
        DispatchQueue.main.async { self.connected = value }
    }
}
