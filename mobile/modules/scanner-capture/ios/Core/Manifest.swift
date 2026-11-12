import Foundation

/// `manifest.json` (CONTRACTS.md §2, schema `capture_manifest.v1`). Pure Foundation for macOS tests.
public struct CaptureManifest: Codable, Equatable {
  public struct Device: Codable, Equatable {
    public var model: String
    public var os: String
    public var app_version: String
    public init(model: String, os: String, app_version: String) {
      self.model = model; self.os = os; self.app_version = app_version
    }
  }
  public struct VideoFormat: Codable, Equatable {
    public var w: Int
    public var h: Int
    public var fps: Int
    public var codec: String
    public init(w: Int, h: Int, fps: Int, codec: String = "hevc") {
      self.w = w; self.h = h; self.fps = fps; self.codec = codec
    }
  }
  public struct Segment: Codable, Equatable {
    public var index: Int
    public var video: String
    public var frames: String
    public var depth: String
    public var imu: String
    public var n_frames: Int
    public var duration_s: Double
    public init(index: Int, n_frames: Int, duration_s: Double) {
      let s = String(format: "seg_%03d", index)
      self.index = index
      video = "video/\(s).mov"; frames = "frames/\(s).jsonl"; depth = "depth/\(s).bin"; imu = "imu/\(s).csv"
      self.n_frames = n_frames; self.duration_s = duration_s
    }
  }
  public struct FileEntry: Codable, Equatable {
    public var path: String
    public var size: Int64
    public var crc32c: String
  }

  public var schema = "capture_manifest.v1"
  public var scan_id: String
  public var name: String
  public var created_at: String
  public var capture_source = "iphone_arkit"
  public var device: Device
  public var video_format: VideoFormat
  public var segments: [Segment]
  public var stills: String?
  public var arkit_mesh: String?
  public var files: [FileEntry]
  public var heading_deg_at_start: Double?

  public init(scan_id: String, name: String, created_at: String, device: Device,
              video_format: VideoFormat, segments: [Segment], stills: String?, arkit_mesh: String?,
              files: [FileEntry] = [], heading_deg_at_start: Double?) {
    self.scan_id = scan_id; self.name = name; self.created_at = created_at; self.device = device
    self.video_format = video_format; self.segments = segments; self.stills = stills
    self.arkit_mesh = arkit_mesh; self.files = files; self.heading_deg_at_start = heading_deg_at_start
  }

  public static func isoNow(_ d: Date = Date()) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: d)
  }
}

public enum ManifestBuilder {
  public static let manifestName = "manifest.json"

  /// Lists every regular, non-hidden file under `dir` (relative POSIX paths, sorted), except manifest.json.
  public static func packageFiles(in dir: URL) -> [String] {
    let root = dir.standardizedFileURL.resolvingSymlinksInPath().path
    guard let en = FileManager.default.enumerator(
      at: dir, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles])
    else { return [] }
    var out: [String] = []
    for case let url as URL in en {
      guard (try? url.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true else { continue }
      let full = url.standardizedFileURL.resolvingSymlinksInPath().path
      guard full.hasPrefix(root + "/") else { continue }
      let rel = String(full.dropFirst(root.count + 1))
      if rel == manifestName { continue }
      out.append(rel)
    }
    return out.sorted()
  }

  /// Fills `files` with size + CRC32C of every package file, drops references to files that don't
  /// exist (e.g. no mesh / no stills), and writes manifest.json atomically.
  @discardableResult
  public static func finalize(_ manifest: CaptureManifest, in dir: URL,
                              progress: ((Int64) -> Void)? = nil) throws -> CaptureManifest {
    var m = manifest
    let fm = FileManager.default
    var files: [CaptureManifest.FileEntry] = []
    var done: Int64 = 0
    for rel in packageFiles(in: dir) {
      let (size, crc) = try CRC32C.file(dir.appendingPathComponent(rel)) { progress?(done + $0) }
      done += size
      files.append(.init(path: rel, size: size, crc32c: CRC32C.base64(crc)))
    }
    m.files = files
    if let s = m.stills, !fm.fileExists(atPath: dir.appendingPathComponent(s).path) { m.stills = nil }
    if let a = m.arkit_mesh, !fm.fileExists(atPath: dir.appendingPathComponent(a).path) { m.arkit_mesh = nil }
    try encode(m).write(to: dir.appendingPathComponent(manifestName), options: .atomic)
    return m
  }

  public static func encode(_ m: CaptureManifest) throws -> Data {
    let enc = JSONEncoder()
    enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes, .prettyPrinted]
    return try enc.encode(m)
  }
}
