import Foundation
import zlib

/// Encoder for `depth/seg_NNN.bin` records (CONTRACTS.md §2):
///   "SDP1" | u32 i | f64 t | u16 w | u16 h | u32 n | zlib(depth f32[w*h] ++ conf u8[w*h])
/// All little-endian. The payload is zlib-wrapped (RFC 1950) so Python's `zlib.decompress` reads it.
public enum DepthRecord {
  public static let magic: [UInt8] = Array("SDP1".utf8)
  public static let headerSize = 4 + 4 + 8 + 2 + 2 + 4

  public enum EncodeError: Error { case sizeMismatch, zlib(Int32) }

  /// `depth` must hold w*h*4 bytes (float32 metres, row-major, no padding); `confidence` w*h bytes.
  public static func encode(frameIndex: UInt32, timestamp: Double, width: Int, height: Int,
                            depth: Data, confidence: Data, level: Int32 = 1) throws -> Data {
    let n = width * height
    guard depth.count == n * 4, confidence.count == n else { throw EncodeError.sizeMismatch }
    var raw = Data(capacity: n * 5)
    raw.append(depth)
    raw.append(confidence)
    let payload = try zlibCompress(raw, level: level)

    var out = Data(capacity: headerSize + payload.count)
    out.append(contentsOf: magic)
    appendLE(&out, frameIndex)
    appendLE(&out, timestamp.bitPattern)
    appendLE(&out, UInt16(width))
    appendLE(&out, UInt16(height))
    appendLE(&out, UInt32(payload.count))
    out.append(payload)
    return out
  }

  public static func zlibCompress(_ input: Data, level: Int32 = 1) throws -> Data {
    var destLen = compressBound(uLong(input.count))
    var out = Data(count: Int(destLen))
    let rc: Int32 = out.withUnsafeMutableBytes { dst in
      input.withUnsafeBytes { src in
        compress2(dst.bindMemory(to: Bytef.self).baseAddress, &destLen,
                  src.bindMemory(to: Bytef.self).baseAddress, uLong(input.count), level)
      }
    }
    guard rc == Z_OK else { throw EncodeError.zlib(rc) }
    out.count = Int(destLen)
    return out
  }

  @inline(__always)
  static func appendLE<T: FixedWidthInteger>(_ d: inout Data, _ v: T) {
    var le = v.littleEndian
    withUnsafeBytes(of: &le) { d.append(contentsOf: $0) }
  }
}
