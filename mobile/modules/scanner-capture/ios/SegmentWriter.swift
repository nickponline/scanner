import ARKit
import AVFoundation
import CoreMotion
import VideoToolbox

/// Writes one recording segment: video/seg_NNN.mov, frames/seg_NNN.jsonl, depth/seg_NNN.bin, imu/seg_NNN.csv.
///
/// Threading: `append(frame:)` is called on the ARSession delegate queue and only does cheap work there
/// (AVAssetWriter append, copying the 256x192 depth maps). Formatting, zlib and file IO run on `io`.
final class SegmentWriter {
  let index: Int
  let root: URL
  private let io: DispatchQueue
  private let depthEvery: Int

  private var writer: AVAssetWriter?
  private var input: AVAssetWriterInput?
  private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
  private var framesFH: FileHandle?
  private var depthFH: FileHandle?
  private var imuFH: FileHandle?
  private var framesBuf = Data()
  private var imuBuf = Data()

  private(set) var t0: Double?
  private(set) var lastT: Double = 0
  private(set) var frameCount = 0
  private(set) var droppedFrames = 0
  private(set) var depthCount = 0
  private(set) var videoSize: (Int, Int) = (0, 0)
  private(set) var failed: Error?
  private var lastPTS = CMTime.negativeInfinity
  private static let timescale: CMTimeScale = 90_000

  var durationS: Double { t0.map { max(0, lastT - $0) } ?? 0 }
  var name: String { Sidecar.segName(index) }

  init(index: Int, root: URL, io: DispatchQueue, depthEvery: Int = 3) throws {
    self.index = index
    self.root = root
    self.io = io
    self.depthEvery = depthEvery
    let fm = FileManager.default
    for d in ["video", "frames", "depth", "imu"] {
      try fm.createDirectory(at: root.appendingPathComponent(d), withIntermediateDirectories: true)
    }
    framesFH = try Self.create(root.appendingPathComponent("frames/\(name).jsonl"))
    depthFH = try Self.create(root.appendingPathComponent("depth/\(name).bin"))
    imuFH = try Self.create(root.appendingPathComponent("imu/\(name).csv"))
    try imuFH?.write(contentsOf: Data(Sidecar.imuHeader.utf8))
  }

  private static func create(_ url: URL) throws -> FileHandle {
    FileManager.default.createFile(atPath: url.path, contents: nil,
                                   attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
    return try FileHandle(forWritingTo: url)
  }

  private func startWriter(width: Int, height: Int) throws {
    let url = root.appendingPathComponent("video/\(name).mov")
    try? FileManager.default.removeItem(at: url)
    let w = try AVAssetWriter(outputURL: url, fileType: .mov)
    // ~50 Mbps at 4K, scaled by pixel count, never below 25 Mbps.
    let bitrate = max(25_000_000, Int(50_000_000.0 * Double(width * height) / (3840.0 * 2160.0)))
    let settings: [String: Any] = [
      AVVideoCodecKey: AVVideoCodecType.hevc,
      AVVideoWidthKey: width,
      AVVideoHeightKey: height,
      AVVideoCompressionPropertiesKey: [
        AVVideoAverageBitRateKey: bitrate,
        AVVideoExpectedSourceFrameRateKey: 30,
        AVVideoMaxKeyFrameIntervalKey: 30,
        AVVideoAllowFrameReorderingKey: false,  // decode order == presentation order == jsonl order
        AVVideoProfileLevelKey: kVTProfileLevel_HEVC_Main_AutoLevel as String,
      ],
    ]
    let inp = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
    inp.expectsMediaDataInRealTime = true
    // Native sensor orientation (landscape): no transform.
    let ad = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: inp, sourcePixelBufferAttributes: nil)
    guard w.canAdd(inp) else { throw NSError(domain: "SegmentWriter", code: 1, userInfo: [NSLocalizedDescriptionKey: "cannot add video input"]) }
    w.add(inp)
    guard w.startWriting() else { throw w.error ?? NSError(domain: "SegmentWriter", code: 2) }
    w.startSession(atSourceTime: .zero)
    writer = w; input = inp; adaptor = ad
    videoSize = (width, height)
  }

  /// Returns the written frame index, or nil if the frame was dropped.
  @discardableResult
  func append(frame: ARFrame) -> Int? {
    guard failed == nil else { return nil }
    let pb = frame.capturedImage
    if writer == nil {
      do { try startWriter(width: CVPixelBufferGetWidth(pb), height: CVPixelBufferGetHeight(pb)) } catch {
        failed = error; return nil
      }
      t0 = frame.timestamp
    }
    guard let t0, let input, let adaptor else { return nil }
    guard input.isReadyForMoreMediaData else { droppedFrames += 1; return nil }
    let pts = CMTime(value: CMTimeValue(((frame.timestamp - t0) * Double(Self.timescale)).rounded()),
                     timescale: Self.timescale)
    guard pts > lastPTS else { droppedFrames += 1; return nil }
    guard adaptor.append(pb, withPresentationTime: pts) else {
      failed = writer?.error ?? NSError(domain: "SegmentWriter", code: 3)
      return nil
    }
    lastPTS = pts
    lastT = frame.timestamp
    let i = frameCount
    frameCount += 1

    // Depth every Nth written frame: copy now (ARKit recycles the buffers), compress on io.
    var depthCopy: (Data, Data, Int, Int)?
    if i % depthEvery == 0, let sd = frame.sceneDepth {
      depthCopy = Self.copyDepth(sd)
    }
    let cam = frame.camera
    let T = Self.flatten(cam.transform)
    let k = cam.intrinsics
    let K: [Float] = [k.columns.0.x, k.columns.1.y, k.columns.2.x, k.columns.2.y]
    let res = cam.imageResolution
    let track = Self.trackString(cam.trackingState)
    let iso = (frame.exifData[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber])?.first?.doubleValue
    let exp = cam.exposureDuration
    let t = frame.timestamp
    let ptsS = Double(pts.value) / Double(Self.timescale)
    let hasDepth = depthCopy != nil
    io.async { [self] in
      if let (d, c, w, h) = depthCopy {
        if let rec = try? DepthRecord.encode(frameIndex: UInt32(i), timestamp: t, width: w, height: h,
                                             depth: d, confidence: c) {
          try? depthFH?.write(contentsOf: rec)
          depthCount += 1
        }
      }
      let line = Sidecar.frameLine(i: i, t: t, pts: ptsS, T: T, K: K, w: Int(res.width), h: Int(res.height),
                                   track: track, exp: exp, iso: iso, depth: hasDepth)
      framesBuf.append(contentsOf: line.utf8)
      if framesBuf.count > 64 * 1024 { flushFrames() }
    }
    return i
  }

  func append(motion m: CMDeviceMotion) {
    let line = Sidecar.imuLine(
      t: m.timestamp,
      a: (m.userAcceleration.x + m.gravity.x, m.userAcceleration.y + m.gravity.y, m.userAcceleration.z + m.gravity.z),
      g: (m.rotationRate.x, m.rotationRate.y, m.rotationRate.z),
      grav: (m.gravity.x, m.gravity.y, m.gravity.z),
      heading: m.heading)
    io.async { [self] in
      imuBuf.append(contentsOf: line.utf8)
      if imuBuf.count > 32 * 1024 { try? imuFH?.write(contentsOf: imuBuf); imuBuf.removeAll(keepingCapacity: true) }
    }
  }

  private func flushFrames() {
    try? framesFH?.write(contentsOf: framesBuf)
    framesBuf.removeAll(keepingCapacity: true)
  }

  /// Finishes the movie and flushes sidecars. Completion runs on `io`.
  func finish(_ done: @escaping (CaptureManifest.Segment) -> Void) {
    let finalize: () -> Void = { [self] in
      io.async { [self] in
        flushFrames()
        try? imuFH?.write(contentsOf: imuBuf); imuBuf.removeAll()
        try? framesFH?.close(); try? depthFH?.close(); try? imuFH?.close()
        framesFH = nil; depthFH = nil; imuFH = nil
        done(CaptureManifest.Segment(index: index, n_frames: frameCount, duration_s: durationS))
      }
    }
    guard let writer, writer.status == .writing else {
      // No frames: write an empty-but-valid layout is impossible for .mov, so leave it absent.
      finalize(); return
    }
    input?.markAsFinished()
    writer.endSession(atSourceTime: lastPTS.isNumeric ? lastPTS : .zero)
    writer.finishWriting { finalize() }
  }

  // MARK: helpers

  static func flatten(_ m: simd_float4x4) -> [Float] {
    [m.columns.0.x, m.columns.0.y, m.columns.0.z, m.columns.0.w,
     m.columns.1.x, m.columns.1.y, m.columns.1.z, m.columns.1.w,
     m.columns.2.x, m.columns.2.y, m.columns.2.z, m.columns.2.w,
     m.columns.3.x, m.columns.3.y, m.columns.3.z, m.columns.3.w]
  }

  static func trackString(_ s: ARCamera.TrackingState) -> String {
    switch s {
    case .normal: return "normal"
    case .notAvailable: return "notAvailable"
    case .limited(let r):
      switch r {
      case .initializing: return "limited:initializing"
      case .excessiveMotion: return "limited:excessiveMotion"
      case .insufficientFeatures: return "limited:insufficientFeatures"
      case .relocalizing: return "limited:relocalizing"
      @unknown default: return "limited:unknown"
      }
    }
  }

  /// Copies depth (Float32) and confidence (UInt8) into tightly packed Data, dropping row padding.
  static func copyDepth(_ sd: ARDepthData) -> (Data, Data, Int, Int)? {
    let dm = sd.depthMap
    guard CVPixelBufferGetPixelFormatType(dm) == kCVPixelFormatType_DepthFloat32 else { return nil }
    let w = CVPixelBufferGetWidth(dm), h = CVPixelBufferGetHeight(dm)
    let depth = copyPlane(dm, rowBytes: w * 4, rows: h)
    var conf: Data
    if let cm = sd.confidenceMap, CVPixelBufferGetWidth(cm) == w, CVPixelBufferGetHeight(cm) == h {
      conf = copyPlane(cm, rowBytes: w, rows: h)
    } else {
      conf = Data(repeating: 2, count: w * h)
    }
    return (depth, conf, w, h)
  }

  private static func copyPlane(_ pb: CVPixelBuffer, rowBytes: Int, rows: Int) -> Data {
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(pb) else { return Data(count: rowBytes * rows) }
    let stride = CVPixelBufferGetBytesPerRow(pb)
    if stride == rowBytes { return Data(bytes: base, count: rowBytes * rows) }
    var out = Data(count: rowBytes * rows)
    out.withUnsafeMutableBytes { dst in
      for r in 0..<rows {
        memcpy(dst.baseAddress! + r * rowBytes, base + r * stride, rowBytes)
      }
    }
    return out
  }
}
