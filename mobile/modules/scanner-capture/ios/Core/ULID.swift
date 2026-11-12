import Foundation

/// Crockford base32 ULID (48-bit ms timestamp + 80 random bits).
public enum ULID {
  private static let alphabet = Array("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

  public static func make(date: Date = Date()) -> String {
    var bytes = [UInt8](repeating: 0, count: 16)
    let ms = UInt64(max(0, date.timeIntervalSince1970 * 1000))
    for i in 0..<6 { bytes[i] = UInt8((ms >> (8 * UInt64(5 - i))) & 0xFF) }
    for i in 6..<16 { bytes[i] = UInt8.random(in: 0...255) }
    // 128 bits -> 26 chars (first char encodes 3 bits).
    var value = bytes
    var out = [Character](repeating: "0", count: 26)
    for idx in stride(from: 25, through: 0, by: -1) {
      var rem: UInt16 = 0
      for j in 0..<16 {
        let cur = (rem << 8) | UInt16(value[j])
        value[j] = UInt8(cur / 32)
        rem = cur % 32
      }
      out[idx] = alphabet[Int(rem)]
    }
    return String(out)
  }
}
