import Foundation

/// CRC-32C (Castagnoli, reflected polynomial 0x82F63B78), slicing-by-8.
/// Matches GCS `crc32c` object metadata / google-crc32c. Pure Swift so it can be unit tested on macOS.
public struct CRC32C {
  private static let tables: [[UInt32]] = {
    var t = [[UInt32]](repeating: [UInt32](repeating: 0, count: 256), count: 8)
    for i in 0..<256 {
      var c = UInt32(i)
      for _ in 0..<8 { c = (c & 1) != 0 ? (c >> 1) ^ 0x82F6_3B78 : (c >> 1) }
      t[0][i] = c
    }
    for i in 0..<256 {
      for k in 1..<8 { t[k][i] = (t[k - 1][i] >> 8) ^ t[0][Int(t[k - 1][i] & 0xFF)] }
    }
    return t
  }()
  private static let flat: [UInt32] = tables.flatMap { $0 }

  private var state: UInt32 = 0xFFFF_FFFF
  public init() {}

  public var value: UInt32 { state ^ 0xFFFF_FFFF }

  public mutating func update(_ data: Data) {
    data.withUnsafeBytes { update(UnsafeRawBufferPointer($0)) }
  }

  public mutating func update(_ buf: UnsafeRawBufferPointer) {
    guard let base = buf.baseAddress, buf.count > 0 else { return }
    var crc = state
    var p = base.assumingMemoryBound(to: UInt8.self)
    var n = buf.count
    CRC32C.flat.withUnsafeBufferPointer { tp in
      let t = tp.baseAddress!
      while n > 0 && (UInt(bitPattern: p) & 7) != 0 {
        crc = t[Int((crc ^ UInt32(p.pointee)) & 0xFF)] ^ (crc >> 8)
        p += 1; n -= 1
      }
      while n >= 8 {
        let w = UnsafeRawPointer(p).load(as: UInt64.self).littleEndian
        let lo = UInt32(truncatingIfNeeded: w) ^ crc
        let hi = UInt32(truncatingIfNeeded: w >> 32)
        crc = t[7 * 256 + Int(lo & 0xFF)] ^ t[6 * 256 + Int((lo >> 8) & 0xFF)]
          ^ t[5 * 256 + Int((lo >> 16) & 0xFF)] ^ t[4 * 256 + Int(lo >> 24)]
          ^ t[3 * 256 + Int(hi & 0xFF)] ^ t[2 * 256 + Int((hi >> 8) & 0xFF)]
          ^ t[1 * 256 + Int((hi >> 16) & 0xFF)] ^ t[0 * 256 + Int(hi >> 24)]
        p += 8; n -= 8
      }
      while n > 0 {
        crc = t[Int((crc ^ UInt32(p.pointee)) & 0xFF)] ^ (crc >> 8)
        p += 1; n -= 1
      }
    }
    state = crc
  }

  public static func checksum(_ data: Data) -> UInt32 {
    var c = CRC32C(); c.update(data); return c.value
  }

  /// GCS format: base64 of the big-endian uint32.
  public static func base64(_ v: UInt32) -> String {
    let be = [UInt8(v >> 24), UInt8((v >> 16) & 0xFF), UInt8((v >> 8) & 0xFF), UInt8(v & 0xFF)]
    return Data(be).base64EncodedString()
  }

  /// Streams a file in 4 MiB blocks. Returns (size, crc).
  public static func file(_ url: URL, progress: ((Int64) -> Void)? = nil) throws -> (Int64, UInt32) {
    let fh = try FileHandle(forReadingFrom: url)
    defer { try? fh.close() }
    var c = CRC32C()
    var total: Int64 = 0
    while true {
      let chunk: Data? = try autoreleasepool { try fh.read(upToCount: 4 << 20) }
      guard let d = chunk, !d.isEmpty else { break }
      c.update(d)
      total += Int64(d.count)
      progress?(total)
    }
    return (total, c.value)
  }
}
