import Foundation

/// Message types. Same numbers as usb_server.py on the PC.
enum MsgType: UInt8 {
    case hello = 1   // PC -> iPad  JSON {"fw":..,"fh":..}
    case frame = 2   // PC -> iPad  one JPEG
    case pen = 3     // iPad -> PC  JSON [[x,y,pressure,tiltX,tiltY,state],...]
    case ack = 4     // iPad -> PC  a frame was received
    case log = 5     // iPad -> PC  UTF-8 text
}

enum Wire {
    /// [4 bytes big-endian length of (type + payload)] [1 byte type] [payload]
    static func pack(_ type: MsgType, _ payload: Data = Data()) -> Data {
        var out = Data()
        let length = UInt32(payload.count + 1)
        out.append(UInt8((length >> 24) & 0xFF))
        out.append(UInt8((length >> 16) & 0xFF))
        out.append(UInt8((length >> 8) & 0xFF))
        out.append(UInt8(length & 0xFF))
        out.append(type.rawValue)
        out.append(payload)
        return out
    }
}

/// Cuts the incoming byte stream into messages.
/// `buffer` is always rebuilt with subdata(), so its indices always start at 0.
struct FrameParser {
    private var buffer = Data()
    private let maxMessage = 32 * 1024 * 1024
    private(set) var broken = false

    mutating func feed(_ data: Data) {
        buffer.append(data)
    }

    mutating func next() -> (type: UInt8, payload: Data)? {
        if broken || buffer.count < 4 { return nil }
        var length = 0
        for i in 0..<4 {
            length = (length << 8) | Int(buffer[i])
        }
        if length < 1 || length > maxMessage {
            broken = true
            return nil
        }
        if buffer.count < 4 + length { return nil }
        let type = buffer[4]
        let payload = buffer.subdata(in: 5..<(4 + length))
        buffer = buffer.subdata(in: (4 + length)..<buffer.count)
        return (type: type, payload: payload)
    }
}

/// One Apple Pencil sample. x and y are 0...1 across the picture, like the web version.
struct PenSample {
    static let hover = 0, contact = 1, out = 2

    var x: Double
    var y: Double
    var pressure: Double
    var tiltX: Int
    var tiltY: Int
    var state: Int

    private func clean(_ v: Double) -> Double {
        return v.isFinite ? v : 0
    }

    var json: String {
        return String(format: "[%.5f,%.5f,%.3f,%d,%d,%d]",
                      clean(x), clean(y), clean(pressure), tiltX, tiltY, state)
    }
}
