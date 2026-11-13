import AVFoundation
import CoreMedia
import UIKit

/// Shows H.264 frames with the iPad's hardware decoder.
/// AVSampleBufferDisplayLayer decodes and displays by itself; we only wrap each frame in a CMSampleBuffer.
/// All methods must be called from one serial queue (USBServer's decodeQueue).
final class H264Player {
    let layer = AVSampleBufferDisplayLayer()

    private var format: CMVideoFormatDescription?
    private var waitingForKeyframe = true
    private var counter: Int64 = 0

    init() {
        layer.videoGravity = .resizeAspect
        layer.backgroundColor = UIColor.black.cgColor
    }

    /// New session: forget the old stream.
    func reset() {
        format = nil
        waitingForKeyframe = true
        layer.flushAndRemoveImage()
    }

    /// SPS and PPS from the PC. `nalLength` is how many bytes precede every NAL unit in a frame (normally 4).
    func configure(sps: Data, pps: Data, nalLength: Int) {
        let spsBytes = [UInt8](sps)
        let ppsBytes = [UInt8](pps)
        var description: CMFormatDescription?
        let status = spsBytes.withUnsafeBufferPointer { s in
            ppsBytes.withUnsafeBufferPointer { p in
                let pointers: [UnsafePointer<UInt8>] = [s.baseAddress!, p.baseAddress!]
                let sizes: [Int] = [spsBytes.count, ppsBytes.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: kCFAllocatorDefault,
                    parameterSetCount: 2,
                    parameterSetPointers: pointers,
                    parameterSetSizes: sizes,
                    nalUnitHeaderLength: Int32(nalLength),
                    formatDescriptionOut: &description)
            }
        }
        if status == noErr, let description = description {
            format = description
            waitingForKeyframe = true
            AppLog.shared.add("H.264 decoder ready")
        } else {
            AppLog.shared.add("H.264 setup failed (\(status))")
        }
    }

    /// One frame in AVCC form (each NAL unit preceded by its length).
    func enqueue(_ data: Data, keyframe: Bool) {
        guard let format = format else { return }

        if layer.status == .failed {
            AppLog.shared.add("Display layer failed, waiting for a keyframe")
            layer.flush()
            waitingForKeyframe = true
        }
        if waitingForKeyframe {
            if !keyframe { return }
            waitingForKeyframe = false
        }

        var block: CMBlockBuffer?
        var status = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: data.count,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: data.count,
            flags: 0,
            blockBufferOut: &block)
        guard status == kCMBlockBufferNoErr, let blockBuffer = block else { return }

        status = data.withUnsafeBytes { raw in
            CMBlockBufferReplaceDataBytes(
                with: raw.baseAddress!,
                blockBuffer: blockBuffer,
                offsetIntoDestination: 0,
                dataLength: data.count)
        }
        guard status == kCMBlockBufferNoErr else { return }

        counter += 1
        var timing = CMSampleTimingInfo(
            duration: CMTime.invalid,
            presentationTimeStamp: CMTime(value: counter, timescale: 60),
            decodeTimeStamp: CMTime.invalid)
        var size = data.count
        var sample: CMSampleBuffer?
        status = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: blockBuffer,
            formatDescription: format,
            sampleCount: 1,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &size,
            sampleBufferOut: &sample)
        guard status == noErr, let sampleBuffer = sample else { return }

        // Show it right away instead of waiting for its timestamp.
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: true),
           CFArrayGetCount(attachments) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(attachments, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(dict,
                                 Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }

        layer.enqueue(sampleBuffer)
    }
}
