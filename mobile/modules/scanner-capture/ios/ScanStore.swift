import Foundation

/// On-device layout:
///   Documents/scans/scan_<ulid>/…            the capture package exactly as uploaded (visible in Files/Finder)
///   Application Support/Scanner/meta/<id>/   local-only: meta.json (owned by JS), thumb.jpg
///   Application Support/Scanner/uploads.json upload state (UploadManager)
enum ScanStore {
  static var packagesRoot: URL {
    let u = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("scans", isDirectory: true)
    try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
    return u
  }

  static var supportRoot: URL {
    let u = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
      .appendingPathComponent("Scanner", isDirectory: true)
    try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
    return u
  }

  static func packageDir(_ id: String) -> URL { packagesRoot.appendingPathComponent(id, isDirectory: true) }
  static func metaDir(_ id: String) -> URL { supportRoot.appendingPathComponent("meta/\(id)", isDirectory: true) }

  static func isValidId(_ id: String) -> Bool {
    id.hasPrefix("scan_") && !id.contains("/") && !id.contains("..")
  }

  static func readMeta(_ id: String) -> [String: Any] {
    guard let d = try? Data(contentsOf: metaDir(id).appendingPathComponent("meta.json")),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
  }

  static func writeMeta(_ id: String, _ meta: [String: Any]) throws {
    try FileManager.default.createDirectory(at: metaDir(id), withIntermediateDirectories: true)
    let d = try JSONSerialization.data(withJSONObject: meta, options: [.sortedKeys])
    try d.write(to: metaDir(id).appendingPathComponent("meta.json"), options: .atomic)
  }

  static func manifest(_ id: String) -> [String: Any]? {
    guard let d = try? Data(contentsOf: packageDir(id).appendingPathComponent(ManifestBuilder.manifestName)) else { return nil }
    return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
  }

  static func dirSize(_ url: URL) -> Int64 {
    var total: Int64 = 0
    if let en = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.fileSizeKey]) {
      for case let u as URL in en { total += Int64((try? u.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0) }
    }
    return total
  }

  static func describe(_ id: String) -> [String: Any] {
    let m = manifest(id)
    let thumb = metaDir(id).appendingPathComponent("thumb.jpg")
    var d: [String: Any] = [
      "scanId": id,
      "dir": packageDir(id).path,
      "sizeBytes": dirSize(packageDir(id)),
      "hasManifest": m != nil,
      "meta": readMeta(id),
    ]
    if let m {
      d["name"] = m["name"]; d["createdAt"] = m["created_at"]
      if let segs = m["segments"] as? [[String: Any]] {
        d["durationS"] = segs.reduce(0.0) { $0 + (($1["duration_s"] as? Double) ?? 0) }
      }
    }
    if FileManager.default.fileExists(atPath: thumb.path) { d["thumbnailUri"] = thumb.absoluteString }
    return d
  }

  static func list() -> [[String: Any]] {
    let ids = (try? FileManager.default.contentsOfDirectory(atPath: packagesRoot.path)) ?? []
    return ids.filter(isValidId).sorted(by: >).map(describe)
  }

  static func delete(_ id: String) throws {
    guard isValidId(id) else { return }
    try? FileManager.default.removeItem(at: packageDir(id))
    try? FileManager.default.removeItem(at: metaDir(id))
  }
}
