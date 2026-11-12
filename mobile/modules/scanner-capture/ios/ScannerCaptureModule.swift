import ARKit
import ExpoModulesCore

public class ScannerCaptureModule: Module {
  private var appVersion: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
  }

  public func definition() -> ModuleDefinition {
    Name("ScannerCapture")

    Events("onStatus", "onMap", "onCaptureEvent", "onUploadProgress", "onUploadState")

    OnCreate {
      let engine = CaptureEngine.shared
      engine.onStatus = { [weak self] in self?.sendEvent("onStatus", $0) }
      engine.onMap = { [weak self] in self?.sendEvent("onMap", $0) }
      engine.onEvent = { [weak self] in self?.sendEvent("onCaptureEvent", $0) }
      let up = UploadManager.shared
      up.onProgress = { [weak self] in self?.sendEvent("onUploadProgress", $0) }
      up.onState = { [weak self] in self?.sendEvent("onUploadState", $0) }
      up.activate()
    }

    OnDestroy {
      CaptureEngine.shared.onStatus = nil
      CaptureEngine.shared.onMap = nil
      CaptureEngine.shared.onEvent = nil
      UploadManager.shared.onProgress = nil
      UploadManager.shared.onState = nil
    }

    // MARK: device

    Function("isSupported") { CaptureEngine.isSupported }

    // Test hook: `SIMCTL_CHILD_SCANNER_INITIAL_ROUTE=/settings xcrun simctl launch …` opens a route
    // without the "Open in Scanner?" prompt that `simctl openurl` shows. Unset in normal launches.
    Function("initialRoute") { () -> String? in ProcessInfo.processInfo.environment["SCANNER_INITIAL_ROUTE"] }

    Function("getDeviceInfo") { () -> [String: Any] in
      let f = CaptureEngine.isSupported ? CaptureEngine.chooseVideoFormat() : nil
      return [
        "model": DeviceInfo.model,
        "os": DeviceInfo.os,
        "appVersion": self.appVersion,
        "supported": CaptureEngine.isSupported,
        "supportsMesh": CaptureEngine.isSupported && CaptureEngine.supportsMesh,
        "videoFormat": f.map { ["w": Int($0.imageResolution.width), "h": Int($0.imageResolution.height),
                                "fps": $0.framesPerSecond] } as Any,
        "freeBytes": (try? ScanStore.packagesRoot.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?
          .volumeAvailableCapacityForImportantUsage ?? 0,
      ]
    }

    // MARK: capture

    AsyncFunction("startSession") {
      try CaptureEngine.shared.startSession()
    }.runOnQueue(.main)

    AsyncFunction("stopSession") {
      CaptureEngine.shared.stopSession()
    }

    AsyncFunction("startRecording") { (opts: StartRecordingOptions) -> [String: Any] in
      var o = CaptureEngine.StartOptions(name: opts.name)
      if let v = opts.stillSpacingM { o.stillSpacingM = v }
      if let v = opts.stillAngleDeg { o.stillAngleDeg = v }
      if let v = opts.stills { o.stills = v }
      let (id, dir) = try CaptureEngine.shared.startRecording(o, root: ScanStore.packagesRoot)
      try? ScanStore.writeMeta(id, ["phase": "recording"])
      return ["scanId": id, "dir": dir.path]
    }

    AsyncFunction("pauseRecording") { try CaptureEngine.shared.pauseRecording() }
    AsyncFunction("resumeRecording") { try CaptureEngine.shared.resumeRecording() }

    AsyncFunction("stopRecording") { (promise: Promise) in
      CaptureEngine.shared.stopRecording(appVersion: self.appVersion) { result in
        switch result {
        case .success(let r):
          try? ScanStore.writeMeta(r.scanId, ["phase": "recorded"])
          promise.resolve(ScanStore.describe(r.scanId))
        case .failure(let e):
          promise.reject("ERR_STOP", e.localizedDescription)
        }
      }
    }

    AsyncFunction("discardRecording") { CaptureEngine.shared.discardRecording() }

    // MARK: local scans

    AsyncFunction("listLocalScans") { ScanStore.list() }

    AsyncFunction("getLocalScan") { (id: String) -> [String: Any]? in
      guard ScanStore.isValidId(id), FileManager.default.fileExists(atPath: ScanStore.packageDir(id).path) else { return nil }
      return ScanStore.describe(id)
    }

    AsyncFunction("readManifest") { (id: String) -> String? in
      guard ScanStore.isValidId(id) else { return nil }
      let url = ScanStore.packageDir(id).appendingPathComponent(ManifestBuilder.manifestName)
      return try? String(contentsOf: url, encoding: .utf8)
    }

    AsyncFunction("setScanMeta") { (id: String, meta: [String: Any]) in
      guard ScanStore.isValidId(id) else { throw CaptureError.badState("bad scan id") }
      try ScanStore.writeMeta(id, meta)
    }

    AsyncFunction("deleteLocalScan") { (id: String) in
      UploadManager.shared.cancel(scanId: id)
      try ScanStore.delete(id)
    }

    // MARK: upload

    AsyncFunction("startUpload") { (id: String, uploads: [UploadTarget], wifiOnly: Bool) in
      try UploadManager.shared.start(scanId: id, uploads: uploads.map { ($0.path, $0.url) }, wifiOnly: wifiOnly)
    }

    AsyncFunction("updateUploadUrls") { (id: String, uploads: [UploadTarget]) in
      UploadManager.shared.updateUrls(scanId: id, uploads: uploads.map { ($0.path, $0.url) })
    }

    AsyncFunction("getUploadState") { (id: String) -> [String: Any]? in UploadManager.shared.state(scanId: id) }
    AsyncFunction("getAllUploadStates") { UploadManager.shared.allStates() }
    AsyncFunction("resumeUploads") { UploadManager.shared.activate() }
    AsyncFunction("setWifiOnly") { (on: Bool) in UploadManager.shared.setWifiOnly(on) }
    AsyncFunction("cancelUpload") { (id: String) in UploadManager.shared.cancel(scanId: id) }
    AsyncFunction("forgetUpload") { (id: String) in UploadManager.shared.forget(scanId: id) }

    // MARK: view

    View(CapturePreviewView.self) {
      Prop("showMesh") { (view: CapturePreviewView, show: Bool) in view.showMesh = show }
    }
  }
}

struct StartRecordingOptions: Record {
  @Field var name: String = "Scan"
  @Field var stillSpacingM: Double?
  @Field var stillAngleDeg: Double?
  @Field var stills: Bool?
}

struct UploadTarget: Record {
  @Field var path: String = ""
  @Field var url: String = ""
}
