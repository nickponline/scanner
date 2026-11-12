import ARKit
import AVFoundation
import CoreImage
import CoreMotion
import UIKit

/// Owns the ARSession and the recording state machine. Singleton so the preview view and the module
/// share one session.
///
/// Queues:
///  - `arQueue`   ARSessionDelegate (never blocked; only cheap per-frame work)
///  - `io`        sidecar formatting, zlib, file writes
///  - `stillQ`    JPEG encoding of high-res stills
///  - `statsQ`    4 Hz status / 1 Hz map events, coverage sampling, manifest + CRC32C on stop
final class CaptureEngine: NSObject, ARSessionDelegate {
  static let shared = CaptureEngine()

  enum State: String { case idle, preview, recording, paused, finishing }

  struct StopResult {
    let scanId: String
    let dir: URL
    let manifest: CaptureManifest
  }

  let session = ARSession()
  private let arQueue = DispatchQueue(label: "xyz.liminal.scanner.ar", qos: .userInteractive)
  private let io = DispatchQueue(label: "xyz.liminal.scanner.io", qos: .userInitiated)
  private let stillQ = DispatchQueue(label: "xyz.liminal.scanner.stills", qos: .utility)
  private let statsQ = DispatchQueue(label: "xyz.liminal.scanner.stats", qos: .utility)
  private let motion = CMMotionManager()
  private let motionQueue: OperationQueue = {
    let q = OperationQueue(); q.maxConcurrentOperationCount = 1; q.qualityOfService = .userInitiated; return q
  }()
  private let ciContext = CIContext(options: [.cacheIntermediates: false])
  private let lock = NSLock()

  // Event sinks (set by the module).
  var onStatus: (([String: Any]) -> Void)?
  var onMap: (([String: Any]) -> Void)?
  var onEvent: (([String: Any]) -> Void)?

  // ---- state guarded by `lock` (read by stats timer, written on arQueue) ----
  private(set) var state: State = .idle
  private var tracking = "notAvailable"
  private var fpsEMA: Double = 0
  private var lastFrameT: Double = 0
  private var linVel: Double = 0
  private var angVel: Double = 0
  private var ambient: Double = 1000
  private var featurePoints = 0
  private var centerDepth: Double = 0
  private var lastPose = simd_float4x4(1)
  private var hasPose = false
  private var distance: Double = 0
  private var trajectory: [Float] = []   // x,z pairs (ARKit top-down)
  private var featurelessSince: Double?
  private var tooCloseSince: Double?
  private var thermal: ProcessInfo.ThermalState = .nominal
  private var stillsCount = 0
  private var totalFrames = 0
  private var totalDropped = 0
  private var elapsedPrev: Double = 0    // completed segments
  private var bytesOnDisk: Int64 = 0
  private var freeBytes: Int64 = 0
  private var coverage: [Float] = []
  private var lastError: String?

  // ---- recording (arQueue) ----
  private var scanId: String?
  private var scanDir: URL?
  private var scanName = ""
  private var createdAt = Date()
  private var segment: SegmentWriter?
  private var segments: [CaptureManifest.Segment] = []
  private var nextSegment = 0
  private var videoFormat = CaptureManifest.VideoFormat(w: 0, h: 0, fps: 30)
  private var headingAtStart: Double?
  private var stillsFH: FileHandle?
  private var stillInFlight = false
  private var lastStillPose: simd_float4x4?
  private var stillSpacingM: Double = 0.75
  private var stillAngleDeg: Double = 20
  private var stillsEnabled = true
  private var thumbWritten = false
  private var wbLockWork: DispatchWorkItem?
  private var pendingSegmentFinishes = DispatchGroup()

  private var statusTimer: DispatchSourceTimer?
  private var mapTick = 0

  static var isSupported: Bool {
    ARWorldTrackingConfiguration.isSupported && ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
  }

  static var supportsMesh: Bool { ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }

  override private init() {
    super.init()
    session.delegate = self
    session.delegateQueue = arQueue
    NotificationCenter.default.addObserver(self, selector: #selector(thermalChanged),
                                           name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
    thermal = ProcessInfo.processInfo.thermalState
  }

  @objc private func thermalChanged() {
    let s = ProcessInfo.processInfo.thermalState
    lock.withLock { thermal = s }
  }

  // MARK: - Session

  static func chooseVideoFormat() -> ARConfiguration.VideoFormat? {
    if let f = ARWorldTrackingConfiguration.recommendedVideoFormatFor4KResolution { return f }
    let all = ARWorldTrackingConfiguration.supportedVideoFormats
    let fast = all.filter { $0.framesPerSecond >= 30 }
    return (fast.isEmpty ? all : fast).max {
      $0.imageResolution.width * $0.imageResolution.height < $1.imageResolution.width * $1.imageResolution.height
    }
  }

  func startSession() throws {
    guard Self.isSupported else { throw CaptureError.unsupported }
    let config = ARWorldTrackingConfiguration()
    config.frameSemantics = [.sceneDepth]
    if Self.supportsMesh { config.sceneReconstruction = .mesh }
    if let f = Self.chooseVideoFormat() { config.videoFormat = f }
    config.worldAlignment = .gravity
    config.isAutoFocusEnabled = true
    config.environmentTexturing = .none
    config.isLightEstimationEnabled = true
    if config.videoFormat.isVideoHDRSupported { config.videoHDRAllowed = false }
    let res = config.videoFormat.imageResolution
    videoFormat = .init(w: Int(res.width), h: Int(res.height), fps: config.videoFormat.framesPerSecond)
    let running = lock.withLock { state != .idle }
    session.run(config, options: running ? [] : [.resetTracking, .removeExistingAnchors])
    configureCaptureDevice()
    lock.withLock { if state == .idle { state = .preview } }
    startTimer()
    DispatchQueue.main.async { UIApplication.shared.isIdleTimerDisabled = true }
  }

  func stopSession() {
    let st = lock.withLock { state }
    guard st == .preview else { return }  // never stop a session that is recording
    session.pause()
    lock.withLock { state = .idle; tracking = "notAvailable" }
    statusTimer?.cancel(); statusTimer = nil
    DispatchQueue.main.async { UIApplication.shared.isIdleTimerDisabled = false }
  }

  /// Caps exposure to limit motion blur; white balance is locked 2 s after recording starts (R3.5).
  private func configureCaptureDevice() {
    guard let dev = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera else { return }
    do {
      try dev.lockForConfiguration()
      // 1/120 s: at a typical 30°/s pan, 1/60 s smears ~20 px on 4K (2570 px focal); trade blur for ISO noise
      let cap = CMTime(value: 1, timescale: 120)
      let maxD = dev.activeFormat.maxExposureDuration
      if dev.isExposureModeSupported(.continuousAutoExposure) {
        dev.activeMaxExposureDuration = CMTimeCompare(cap, maxD) < 0 ? cap : maxD
      }
      if dev.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) { dev.whiteBalanceMode = .continuousAutoWhiteBalance }
      dev.unlockForConfiguration()
    } catch {
      NSLog("[ScannerCapture] capture device config failed: \(error)")
    }
  }

  private func setWhiteBalanceLocked(_ locked: Bool) {
    guard let dev = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera else { return }
    let mode: AVCaptureDevice.WhiteBalanceMode = locked ? .locked : .continuousAutoWhiteBalance
    guard dev.isWhiteBalanceModeSupported(mode), (try? dev.lockForConfiguration()) != nil else { return }
    dev.whiteBalanceMode = mode
    dev.unlockForConfiguration()
  }

  // MARK: - Recording

  struct StartOptions {
    var name: String
    var stillSpacingM: Double = 0.75
    var stillAngleDeg: Double = 20
    var stills = true
  }

  func startRecording(_ opts: StartOptions, root: URL) throws -> (String, URL) {
    if lock.withLock({ state == .idle }) { try startSession() }
    return try arQueue.sync {
      guard lock.withLock({ state == .preview }) else { throw CaptureError.badState("already recording") }
      let id = "scan_" + ULID.make()
      let dir = root.appendingPathComponent(id, isDirectory: true)
      try FileManager.default.createDirectory(at: dir.appendingPathComponent("stills"), withIntermediateDirectories: true)
      var rv = URLResourceValues(); rv.isExcludedFromBackup = true
      var d = dir; try? d.setResourceValues(rv)
      FileManager.default.createFile(atPath: dir.appendingPathComponent("stills/stills.jsonl").path, contents: nil)
      stillsFH = try FileHandle(forWritingTo: dir.appendingPathComponent("stills/stills.jsonl"))
      scanId = id; scanDir = dir; scanName = opts.name; createdAt = Date()
      segments = []; nextSegment = 0; headingAtStart = nil
      headingSin = 0; headingCos = 0; headingN = 0
      stillSpacingM = opts.stillSpacingM; stillAngleDeg = opts.stillAngleDeg; stillsEnabled = opts.stills
      lastStillPose = nil; stillInFlight = false; thumbWritten = false
      pendingSegmentFinishes = DispatchGroup()
      lock.withLock {
        stillsCount = 0; totalFrames = 0; totalDropped = 0; elapsedPrev = 0; distance = 0
        trajectory = []; coverage = []; lastError = nil; bytesOnDisk = 0
      }
      try openSegment()
      startMotion()
      lock.withLock { state = .recording }
      scheduleWBLock()
      return (id, dir)
    }
  }

  private func openSegment() throws {
    guard let dir = scanDir else { return }
    segment = try SegmentWriter(index: nextSegment, root: dir, io: io)
    nextSegment += 1
  }

  private func scheduleWBLock() {
    wbLockWork?.cancel()
    let w = DispatchWorkItem { [weak self] in self?.setWhiteBalanceLocked(true) }
    wbLockWork = w
    DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 2, execute: w)
  }

  func pauseRecording() throws {
    try arQueue.sync {
      guard lock.withLock({ state == .recording }) else { throw CaptureError.badState("not recording") }
      lock.withLock { state = .paused }
      closeSegment()
      emit(["type": "paused", "segments": nextSegment])
    }
  }

  func resumeRecording() throws {
    try arQueue.sync {
      guard lock.withLock({ state == .paused }) else { throw CaptureError.badState("not paused") }
      try openSegment()
      lock.withLock { state = .recording }
      emit(["type": "resumed", "segment": nextSegment - 1])
    }
  }

  /// arQueue only.
  private func closeSegment() {
    guard let seg = segment else { return }
    segment = nil
    lock.withLock { elapsedPrev += seg.durationS; totalDropped += seg.droppedFrames }
    pendingSegmentFinishes.enter()
    seg.finish { [weak self] s in
      guard let self else { return }
      self.arQueue.async {
        if s.n_frames > 0 { self.segments.append(s) } else { self.removeEmptySegment(s) }
        self.pendingSegmentFinishes.leave()
      }
    }
  }

  private func removeEmptySegment(_ s: CaptureManifest.Segment) {
    guard let dir = scanDir else { return }
    for p in [s.video, s.frames, s.depth, s.imu] { try? FileManager.default.removeItem(at: dir.appendingPathComponent(p)) }
  }

  func stopRecording(appVersion: String, completion: @escaping (Result<StopResult, Error>) -> Void) {
    arQueue.async { [self] in
      let st = lock.withLock { state }
      guard st == .recording || st == .paused, let id = scanId, let dir = scanDir else {
        completion(.failure(CaptureError.badState("not recording"))); return
      }
      lock.withLock { state = .finishing }
      wbLockWork?.cancel()
      closeSegment()
      stopMotion()
      let meshAnchors = session.currentFrame?.anchors.compactMap { $0 as? ARMeshAnchor } ?? []
      let group = pendingSegmentFinishes
      group.notify(queue: statsQ) { [self] in
        // Wait for in-flight still + io drain.
        stillQ.sync {}
        io.sync {}
        try? stillsFH?.close(); stillsFH = nil
        setWhiteBalanceLocked(false)
        var meshPath: String?
        if !meshAnchors.isEmpty {
          do {
            try MeshExport.write(meshAnchors, to: dir.appendingPathComponent("arkit_mesh.ply"))
            meshPath = "arkit_mesh.ply"
          } catch { NSLog("[ScannerCapture] mesh export failed: \(error)") }
        }
        let segs = arQueue.sync { segments.sorted { $0.index < $1.index } }
        let manifest = CaptureManifest(
          scan_id: id, name: scanName, created_at: CaptureManifest.isoNow(createdAt),
          device: .init(model: DeviceInfo.model, os: DeviceInfo.os, app_version: appVersion),
          video_format: videoFormat, segments: segs,
          stills: stillsCount > 0 ? "stills/stills.jsonl" : nil, arkit_mesh: meshPath,
          heading_deg_at_start: headingAtStart)
        if stillsCount == 0 { try? FileManager.default.removeItem(at: dir.appendingPathComponent("stills")) }
        emit(["type": "finalizing"])
        do {
          let final = try ManifestBuilder.finalize(manifest, in: dir)
          arQueue.async { [self] in
            scanId = nil; scanDir = nil; segment = nil
            lock.withLock { state = .preview }
          }
          completion(.success(StopResult(scanId: id, dir: dir, manifest: final)))
        } catch {
          arQueue.async { [self] in lock.withLock { state = .preview } }
          completion(.failure(error))
        }
      }
    }
  }

  /// Abandons the current recording and deletes its files.
  func discardRecording() {
    stopRecording(appVersion: "0") { r in
      if case .success(let s) = r { try? FileManager.default.removeItem(at: s.dir) }
    }
  }

  // MARK: - Motion

  private func startMotion() {
    guard motion.isDeviceMotionAvailable else { return }
    motion.deviceMotionUpdateInterval = 1.0 / 100.0
    let frames = CMMotionManager.availableAttitudeReferenceFrames()
    let ref: CMAttitudeReferenceFrame = frames.contains(.xMagneticNorthZVertical) ? .xMagneticNorthZVertical : .xArbitraryZVertical
    let hasNorth = ref == .xMagneticNorthZVertical
    motion.startDeviceMotionUpdates(using: ref, to: motionQueue) { [weak self] m, _ in
      guard let self, let m else { return }
      if !hasNorth { self.headingN = Int.max }
      // Route into the current segment (arQueue owns `segment`, but SegmentWriter.append(motion:)
      // only enqueues onto io, so a racy read of the reference is benign).
      self.accumulateHeading(m)
      if let seg = self.currentSegmentForMotion { seg.append(motion: m) }
    }
  }

  // Bearing of the ARKit world −Z axis (degrees clockwise from magnetic north), estimated as
  // bearing(camera forward from CoreMotion) − yaw(camera forward in ARKit), circular-averaged over
  // the first ~2 s of valid samples. Motion queue only.
  private var headingSin = 0.0, headingCos = 0.0, headingN = 0

  private func accumulateHeading(_ m: CMDeviceMotion) {
    guard headingN < 200, motion.deviceMotionUpdateInterval > 0 else { return }
    guard let camBearing = Self.cameraBearing(m) else { return }
    let (yaw, ok): (Double, Bool) = lock.withLock {
      let f = -simd_make_float3(lastPose.columns.2)
      let horiz = hypot(Double(f.x), Double(f.z))
      return (atan2(Double(f.x), Double(-f.z)), hasPose && tracking == "normal" && horiz > 0.5)
    }
    guard ok else { return }
    let b = camBearing - yaw
    headingSin += sin(b); headingCos += cos(b); headingN += 1
    var deg = atan2(headingSin, headingCos) * 180 / .pi
    if deg < 0 { deg += 360 }
    headingAtStart = deg
  }

  /// Bearing (radians, clockwise from magnetic north) of the back camera's optical axis (device −Z).
  /// Resolves CMRotationMatrix's row/column convention by checking which one reproduces gravity.
  static func cameraBearing(_ m: CMDeviceMotion) -> Double? {
    let r = m.attitude.rotationMatrix
    let g = m.gravity
    // Reference frame: X = magnetic north, Z = up, Y = west.
    let rowsAreRefAxes = -(r.m31 * g.x + r.m32 * g.y + r.m33 * g.z) >= -(r.m13 * g.x + r.m23 * g.y + r.m33 * g.z)
    let (north, west) = rowsAreRefAxes ? (-r.m13, -r.m23) : (-r.m31, -r.m32)
    guard hypot(north, west) > 0.5 else { return nil }  // camera pointing at floor/ceiling
    return atan2(-west, north)
  }

  private var currentSegmentForMotion: SegmentWriter? {
    lock.withLock { state == .recording } ? segment : nil
  }

  private func stopMotion() { motion.stopDeviceMotionUpdates() }

  // MARK: - ARSessionDelegate

  func session(_ session: ARSession, didUpdate frame: ARFrame) {
    let cam = frame.camera
    let pose = cam.transform
    let t = frame.timestamp
    let track = SegmentWriter.trackString(cam.trackingState)
    let isNormal: Bool = { if case .normal = cam.trackingState { return true }; return false }()

    var recording = false
    lock.withLock {
      let dt = t - lastFrameT
      if lastFrameT > 0, dt > 0, dt < 1 {
        fpsEMA = fpsEMA == 0 ? 1 / dt : fpsEMA * 0.9 + (1 / dt) * 0.1
        if hasPose {
          let p0 = simd_make_float3(lastPose.columns.3), p1 = simd_make_float3(pose.columns.3)
          let step = Double(simd_distance(p0, p1))
          let ang = Self.rotationAngle(lastPose, pose)
          linVel = linVel * 0.8 + (step / dt) * 0.2
          angVel = angVel * 0.8 + (ang / dt) * 0.2
          if (state == .recording) && isNormal && step < 0.5 { distance += step }
        }
      }
      lastFrameT = t
      lastPose = pose; hasPose = true
      tracking = track
      ambient = Double(frame.lightEstimate?.ambientIntensity ?? 1000)
      featurePoints = frame.rawFeaturePoints?.points.count ?? 0
      if isNormal {
        let p = simd_make_float3(pose.columns.3)
        let n = trajectory.count
        if n < 2 || hypot(Double(p.x - trajectory[n - 2]), Double(p.z - trajectory[n - 1])) > 0.1 {
          trajectory.append(p.x); trajectory.append(p.z)
          if trajectory.count > 20_000 { trajectory = Self.decimate(trajectory) }
        }
      }
      featurelessSince = featurePoints < 40 ? (featurelessSince ?? t) : nil
      recording = state == .recording
    }

    if frame.sceneDepth != nil, Int(t * 30) % 10 == 0 { sampleCenterDepth(frame) }

    guard recording, let seg = segment else { return }
    if let i = seg.append(frame: frame) {
      lock.withLock { totalFrames += 1 }
      if i == 0 && !thumbWritten { writeThumbnail(frame.capturedImage) }
    } else if let err = seg.failed {
      lock.withLock { lastError = err.localizedDescription }
      emit(["type": "error", "message": "video writer failed: \(err.localizedDescription)"])
      lock.withLock { state = .paused }
      closeSegment()
    }
    maybeCaptureStill(pose: pose, isNormal: isNormal)
  }

  func session(_ session: ARSession, didFailWithError error: Error) {
    lock.withLock { lastError = error.localizedDescription; tracking = "notAvailable" }
    emit(["type": "error", "message": error.localizedDescription])
  }

  func sessionWasInterrupted(_ session: ARSession) {
    emit(["type": "interrupted"])
    arQueue.async { [self] in
      if lock.withLock({ state == .recording }) { lock.withLock { state = .paused }; closeSegment() }
    }
  }

  func sessionInterruptionEnded(_ session: ARSession) { emit(["type": "interruptionEnded"]) }

  private func sampleCenterDepth(_ frame: ARFrame) {
    guard let dm = frame.sceneDepth?.depthMap else { return }
    CVPixelBufferLockBaseAddress(dm, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(dm, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(dm) else { return }
    let w = CVPixelBufferGetWidth(dm), h = CVPixelBufferGetHeight(dm), rb = CVPixelBufferGetBytesPerRow(dm)
    var vals: [Float] = []
    for y in stride(from: h / 3, to: 2 * h / 3, by: 8) {
      let row = (base + y * rb).assumingMemoryBound(to: Float.self)
      for x in stride(from: w / 3, to: 2 * w / 3, by: 8) where row[x].isFinite { vals.append(row[x]) }
    }
    guard !vals.isEmpty else { return }
    vals.sort()
    let med = Double(vals[vals.count / 2])
    let t = frame.timestamp
    lock.withLock {
      centerDepth = med
      tooCloseSince = med < 0.4 ? (tooCloseSince ?? t) : nil
    }
  }

  // MARK: - Stills (R3.6)

  private func maybeCaptureStill(pose: simd_float4x4, isNormal: Bool) {
    guard stillsEnabled, !stillInFlight, isNormal else { return }
    let th = lock.withLock { thermal }
    if th == .serious || th == .critical { return }
    let factor = th == .fair ? 2.0 : 1.0
    if let last = lastStillPose {
      let d = Double(simd_distance(simd_make_float3(last.columns.3), simd_make_float3(pose.columns.3)))
      let a = Self.rotationAngle(last, pose) * 180 / .pi
      guard d >= stillSpacingM * factor || a >= stillAngleDeg * factor else { return }
    }
    stillInFlight = true
    lastStillPose = pose
    let segIndex = max(0, nextSegment - 1)
    session.captureHighResolutionFrame { [weak self] frame, error in
      guard let self else { return }
      guard let frame else {
        self.arQueue.async { self.stillInFlight = false }
        if let error { NSLog("[ScannerCapture] hi-res capture failed: \(error)") }
        return
      }
      self.stillQ.async { self.saveStill(frame, seg: segIndex) }
    }
  }

  private func saveStill(_ frame: ARFrame, seg: Int) {
    defer { arQueue.async { self.stillInFlight = false } }
    guard let dir = scanDir, let fh = stillsFH else { return }
    let n = lock.withLock { stillsCount }
    let rel = String(format: "stills/%06d.jpg", n)
    let img = CIImage(cvPixelBuffer: frame.capturedImage)
    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let jpg = ciContext.jpegRepresentation(
            of: img, colorSpace: cs,
            options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.92])
    else { return }
    do { try jpg.write(to: dir.appendingPathComponent(rel)) } catch { return }
    let cam = frame.camera
    let k = cam.intrinsics
    let line = Sidecar.stillLine(file: rel, t: frame.timestamp, T: SegmentWriter.flatten(cam.transform),
                                 K: [k.columns.0.x, k.columns.1.y, k.columns.2.x, k.columns.2.y],
                                 w: Int(cam.imageResolution.width), h: Int(cam.imageResolution.height), seg: seg)
    try? fh.write(contentsOf: Data(line.utf8))
    lock.withLock { stillsCount += 1 }
  }

  private func writeThumbnail(_ pb: CVPixelBuffer) {
    thumbWritten = true
    guard let id = scanId else { return }
    let img = CIImage(cvPixelBuffer: pb).oriented(.right)
    stillQ.async { [self] in
      let s = 480 / max(img.extent.width, img.extent.height)
      let scaled = img.transformed(by: CGAffineTransform(scaleX: s, y: s))
      guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
            let jpg = ciContext.jpegRepresentation(of: scaled, colorSpace: cs, options: [:]) else { return }
      let url = ScanStore.metaDir(id).appendingPathComponent("thumb.jpg")
      try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
      try? jpg.write(to: url)
    }
  }

  // MARK: - Status / map events

  private func startTimer() {
    guard statusTimer == nil else { return }
    let t = DispatchSource.makeTimerSource(queue: statsQ)
    t.schedule(deadline: .now() + 0.25, repeating: 0.25)
    t.setEventHandler { [weak self] in self?.tick() }
    t.resume()
    statusTimer = t
  }

  private func tick() {
    mapTick += 1
    if mapTick % 4 == 0 { refreshDiskAndCoverage() }
    let (status, map) = lock.withLock { () -> ([String: Any], [String: Any]?) in
      var warnings: [String] = []
      // tuned for sharp 4K frames (not just tracking): ~0.6 m/s or ~35°/s already blurs at 1/120 s in dim rooms
      if linVel > 0.6 || angVel > 0.6 { warnings.append("fast_motion") }
      if ambient < 300 { warnings.append("low_light") }
      if tracking.hasPrefix("limited") { warnings.append("tracking_limited") }
      if tracking == "notAvailable" && state != .idle { warnings.append("tracking_lost") }
      if lastFrameT > 0, let s = featurelessSince, lastFrameT - s > 3 { warnings.append("featureless") }
      if lastFrameT > 0, let s = tooCloseSince, lastFrameT - s > 3 { warnings.append("too_close") }
      if thermal == .serious { warnings.append("thermal_serious") }
      if thermal == .critical { warnings.append("thermal_critical") }
      if freeBytes > 0 && freeBytes < 2_000_000_000 { warnings.append("storage_low") }
      let segElapsed = (state == .recording) ? (segment?.durationS ?? 0) : 0
      let p = lastPose.columns.3
      let fwd = -simd_make_float3(lastPose.columns.2)
      var s: [String: Any] = [
        "state": state.rawValue,
        "tracking": tracking,
        "fps": (fpsEMA * 10).rounded() / 10,
        "frames": totalFrames,
        "droppedFrames": totalDropped + (segment?.droppedFrames ?? 0),
        "stills": stillsCount,
        "segments": nextSegment,
        "elapsedS": elapsedPrev + segElapsed,
        "distanceM": distance,
        "bytesWritten": bytesOnDisk,
        "freeBytes": freeBytes,
        "thermal": ["nominal", "fair", "serious", "critical"][min(3, thermal.rawValue)],
        "ambientLux": ambient,
        "linearVelocity": linVel,
        "angularVelocity": angVel,
        "warnings": warnings,
        "pose": ["x": p.x, "y": p.y, "z": p.z, "yaw": atan2(Double(fwd.x), Double(-fwd.z))],
      ]
      if let e = lastError { s["error"] = e }
      let m: [String: Any]? = mapTick % 4 == 0
        ? ["trajectory": trajectory, "coverage": coverage, "cell": 0.25] : nil
      return (s, m)
    }
    onStatus?(status)
    if let map { onMap?(map) }
  }

  private func refreshDiskAndCoverage() {
    let dir: URL? = lock.withLock { state == .idle ? nil : scanDir }
    var bytes: Int64 = 0
    if let dir, let en = FileManager.default.enumerator(at: dir, includingPropertiesForKeys: [.fileSizeKey]) {
      for case let u as URL in en { bytes += Int64((try? u.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0) }
    }
    let free = (try? ScanStore.packagesRoot.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?
      .volumeAvailableCapacityForImportantUsage ?? 0
    let cells = Self.coverageCells(session.currentFrame?.anchors.compactMap { $0 as? ARMeshAnchor } ?? [], cell: 0.25)
    lock.withLock { bytesOnDisk = bytes; freeBytes = free; coverage = cells }
  }

  /// Top-down occupancy of ARKit mesh vertices on an XZ grid. Returns flat [x,z,...] cell centres.
  static func coverageCells(_ anchors: [ARMeshAnchor], cell: Float, maxCells: Int = 6000) -> [Float] {
    var set = Set<Int64>()
    for a in anchors {
      let g = a.geometry
      let src = g.vertices
      let stepV = max(1, src.count / 4000)
      let ptr = src.buffer.contents().advanced(by: src.offset)
      let m = a.transform
      var i = 0
      while i < src.count {
        let v = ptr.advanced(by: i * src.stride).assumingMemoryBound(to: (Float, Float, Float).self).pointee
        let w = m * simd_float4(v.0, v.1, v.2, 1)
        let cx = Int64((w.x / cell).rounded(.down)), cz = Int64((w.z / cell).rounded(.down))
        set.insert((cx << 32) ^ (cz & 0xFFFF_FFFF))
        i += stepV
      }
      if set.count > maxCells { break }
    }
    var out: [Float] = []
    out.reserveCapacity(set.count * 2)
    for k in set {
      let cx = Int32(truncatingIfNeeded: k >> 32), cz = Int32(truncatingIfNeeded: k)
      out.append((Float(cx) + 0.5) * cell); out.append((Float(cz) + 0.5) * cell)
    }
    return out
  }

  // MARK: - utils

  private func emit(_ e: [String: Any]) { onEvent?(e) }

  static func rotationAngle(_ a: simd_float4x4, _ b: simd_float4x4) -> Double {
    let ra = simd_float3x3(simd_make_float3(a.columns.0), simd_make_float3(a.columns.1), simd_make_float3(a.columns.2))
    let rb = simd_float3x3(simd_make_float3(b.columns.0), simd_make_float3(b.columns.1), simd_make_float3(b.columns.2))
    let r = ra.transpose * rb
    let tr = Double(r.columns.0.x + r.columns.1.y + r.columns.2.z)
    return acos(max(-1, min(1, (tr - 1) / 2)))
  }

  static func decimate(_ xz: [Float]) -> [Float] {
    var out: [Float] = []
    out.reserveCapacity(xz.count / 2)
    var i = 0
    while i + 1 < xz.count { out.append(xz[i]); out.append(xz[i + 1]); i += 4 }
    return out
  }
}

enum CaptureError: LocalizedError {
  case unsupported
  case badState(String)
  var errorDescription: String? {
    switch self {
    case .unsupported: return "This device has no LiDAR scene depth support"
    case .badState(let s): return s
    }
  }
}

enum DeviceInfo {
  static var model: String {
    var u = utsname(); uname(&u)
    let m = withUnsafeBytes(of: &u.machine) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
    if m == "arm64" || m == "x86_64" { return ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] ?? m }
    return m
  }
  static var os: String { "iOS \(UIDevice.current.systemVersion)" }
}

enum MeshExport {
  /// Merges ARMeshAnchors into one binary PLY in the ARKit world frame.
  static func write(_ anchors: [ARMeshAnchor], to url: URL) throws {
    var parts: [PLYWriter.Part] = []
    for a in anchors {
      let g = a.geometry
      let vs = g.vertices
      let vp = vs.buffer.contents().advanced(by: vs.offset)
      var verts = [Float](); verts.reserveCapacity(vs.count * 3)
      let m = a.transform
      for i in 0..<vs.count {
        let v = vp.advanced(by: i * vs.stride).assumingMemoryBound(to: (Float, Float, Float).self).pointee
        let w = m * simd_float4(v.0, v.1, v.2, 1)
        verts.append(w.x); verts.append(w.y); verts.append(w.z)
      }
      let f = g.faces
      let fp = f.buffer.contents()
      var faces = [UInt32](); faces.reserveCapacity(f.count * 3)
      let n = f.count * f.indexCountPerPrimitive
      if f.bytesPerIndex == 4 {
        let p = fp.assumingMemoryBound(to: UInt32.self)
        for i in 0..<n { faces.append(p[i]) }
      } else {
        let p = fp.assumingMemoryBound(to: UInt16.self)
        for i in 0..<n { faces.append(UInt32(p[i])) }
      }
      parts.append(.init(vertices: verts, faces: faces))
    }
    try PLYWriter.write(parts, to: url)
  }
}
