import Foundation

/// Line formatters for frames/*.jsonl, stills/stills.jsonl and imu/*.csv (CONTRACTS.md §2).
/// Hand-formatted (no JSONSerialization) because this runs 30x/s on the ARSession queue.
public enum Sidecar {
  @inline(__always)
  static func num(_ v: Double, _ fmt: String = "%.9g") -> String {
    v.isFinite ? String(format: fmt, v) : "null"
  }
  static func arr(_ v: [Float]) -> String {
    "[" + v.map { num(Double($0)) }.joined(separator: ",") + "]"
  }
  static func esc(_ s: String) -> String {
    var o = ""
    for c in s.unicodeScalars {
      switch c {
      case "\"": o += "\\\""
      case "\\": o += "\\\\"
      case "\n": o += "\\n"
      default: if c.value < 0x20 { o += String(format: "\\u%04x", c.value) } else { o.unicodeScalars.append(c) }
      }
    }
    return o
  }

  /// `T`: 16 floats column-major camera→world. `K`: [fx, fy, cx, cy].
  public static func frameLine(i: Int, t: Double, pts: Double, T: [Float], K: [Float], w: Int, h: Int,
                               track: String, exp: Double, iso: Double?, depth: Bool) -> String {
    "{\"i\":\(i),\"t\":\(num(t, "%.6f")),\"pts\":\(num(pts, "%.6f")),\"T\":\(arr(T)),\"K\":\(arr(K)),"
      + "\"w\":\(w),\"h\":\(h),\"track\":\"\(esc(track))\",\"exp\":\(num(exp)),"
      + "\"iso\":\(iso.map { num($0) } ?? "null"),\"depth\":\(depth)}\n"
  }

  public static func stillLine(file: String, t: Double, T: [Float], K: [Float], w: Int, h: Int, seg: Int) -> String {
    "{\"file\":\"\(esc(file))\",\"t\":\(num(t, "%.6f")),\"T\":\(arr(T)),\"K\":\(arr(K)),\"w\":\(w),\"h\":\(h),\"seg\":\(seg)}\n"
  }

  public static let imuHeader = "t,ax,ay,az,gx,gy,gz,grav_x,grav_y,grav_z,heading_deg\n"

  /// a = total specific force (userAcceleration + gravity) in g, device frame (CoreMotion convention);
  /// g = rotation rate rad/s; grav = gravity in g; heading = degrees from magnetic north (-1 if unknown).
  public static func imuLine(t: Double, a: (Double, Double, Double), g: (Double, Double, Double),
                             grav: (Double, Double, Double), heading: Double) -> String {
    let f = "%.6f"
    return [num(t, f), num(a.0, f), num(a.1, f), num(a.2, f), num(g.0, f), num(g.1, f), num(g.2, f),
            num(grav.0, f), num(grav.1, f), num(grav.2, f), num(heading, "%.2f")].joined(separator: ",") + "\n"
  }

  public static func segName(_ i: Int) -> String { String(format: "seg_%03d", i) }
}
