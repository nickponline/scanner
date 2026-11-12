// Standalone macOS test driver for the pure capture helpers in ../ios/Core.
// Built by run.sh with: swiftc -O ../ios/Core/*.swift main.swift
import Foundation

func fail(_ m: String) -> Never { FileHandle.standardError.write(Data("FAIL: \(m)\n".utf8)); exit(1) }

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "./out")
let pkg = out.appendingPathComponent("scan_test")
try? FileManager.default.removeItem(at: pkg)
for d in ["video", "frames", "depth", "stills", "imu"] {
  try! FileManager.default.createDirectory(at: pkg.appendingPathComponent(d), withIntermediateDirectories: true)
}

// 1. CRC32C known vectors.
if CRC32C.checksum(Data("123456789".utf8)) != 0xE306_9283 { fail("crc32c check value") }
if CRC32C.checksum(Data()) != 0 { fail("crc32c empty") }
// Incremental == one-shot across unaligned splits.
var big = Data(count: 1_000_003)
for i in 0..<big.count { big[i] = UInt8((i &* 2_654_435_761) >> 13 & 0xFF) }
var inc = CRC32C()
inc.update(big.subdata(in: 0..<7)); inc.update(big.subdata(in: 7..<500_001)); inc.update(big.subdata(in: 500_001..<big.count))
if inc.value != CRC32C.checksum(big) { fail("crc32c incremental") }
print("crc32c(123456789)=\(String(format: "%08x", CRC32C.checksum(Data("123456789".utf8)))) b64=\(CRC32C.base64(0xE306_9283))")

// 2. Fake "video" (5 MiB + odd tail, crosses the 4 MiB streaming block).
var vid = Data(count: 5 * 1024 * 1024 + 13)
for i in 0..<vid.count { vid[i] = UInt8(truncatingIfNeeded: i &* 31 &+ (i >> 9)) }
try! vid.write(to: pkg.appendingPathComponent("video/seg_000.mov"))

// 3. Depth records: deterministic values the python checker recomputes.
func depthValue(_ rec: Int, _ k: Int) -> Float { Float(rec) + Float(k) * 0.001 }
func confValue(_ rec: Int, _ k: Int) -> UInt8 { UInt8((rec + k) % 3) }
let dims = [(4, 3), (256, 192), (256, 192)]
var depthFile = Data()
var frames = ""
for (r, (w, h)) in dims.enumerated() {
  var d = Data(capacity: w * h * 4)
  var c = Data(capacity: w * h)
  for k in 0..<(w * h) {
    var v = depthValue(r, k).bitPattern.littleEndian
    withUnsafeBytes(of: &v) { d.append(contentsOf: $0) }
    c.append(confValue(r, k))
  }
  let rec = try! DepthRecord.encode(frameIndex: UInt32(r * 3), timestamp: 1000.5 + Double(r) / 10,
                                    width: w, height: h, depth: d, confidence: c)
  depthFile.append(rec)
}
try! depthFile.write(to: pkg.appendingPathComponent("depth/seg_000.bin"))
let I: [Float] = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0.5, 1.25, -2, 1]
for i in 0..<9 {
  frames += Sidecar.frameLine(i: i, t: 1000.5 + Double(i) / 30, pts: Double(i) / 30, T: I,
                              K: [1500.25, 1500.25, 959.5, 719.5], w: 1920, h: 1440,
                              track: i == 4 ? "limited:excessiveMotion" : "normal",
                              exp: 0.008333, iso: i == 5 ? nil : 400, depth: i % 3 == 0)
}
try! frames.write(to: pkg.appendingPathComponent("frames/seg_000.jsonl"), atomically: true, encoding: .utf8)
try! Sidecar.stillLine(file: "stills/000000.jpg", t: 1000.6, T: I, K: [3000, 3000, 2016, 1512], w: 4032, h: 3024, seg: 0)
  .write(to: pkg.appendingPathComponent("stills/stills.jsonl"), atomically: true, encoding: .utf8)
try! Data([0xFF, 0xD8, 0xFF, 0xD9]).write(to: pkg.appendingPathComponent("stills/000000.jpg"))
var imu = Sidecar.imuHeader
for i in 0..<100 {
  imu += Sidecar.imuLine(t: 1000 + Double(i) / 100, a: (0.01, -1.0, 0.02), g: (0.1, 0.2, 0.3),
                         grav: (0, -1, 0), heading: 123.45)
}
try! imu.write(to: pkg.appendingPathComponent("imu/seg_000.csv"), atomically: true, encoding: .utf8)

// 4. PLY: two parts (a triangle + a quad) to check index rebasing.
try! PLYWriter.write([
  .init(vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], faces: [0, 1, 2]),
  .init(vertices: [0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1], faces: [0, 1, 2, 0, 2, 3]),
], to: pkg.appendingPathComponent("arkit_mesh.ply"))

// 5. Hidden files must be excluded.
try! Data("x".utf8).write(to: pkg.appendingPathComponent(".DS_Store"))

// 6. Manifest.
let id = "scan_" + ULID.make()
if id.count != 31 { fail("ulid length \(id)") }
var m = CaptureManifest(
  scan_id: id, name: "Test \"flat\"", created_at: CaptureManifest.isoNow(),
  device: .init(model: "iPhone16,1", os: "iOS 18.1", app_version: "0.1.0"),
  video_format: .init(w: 1920, h: 1440, fps: 30),
  segments: [.init(index: 0, n_frames: 9, duration_s: 0.3)],
  stills: "stills/stills.jsonl", arkit_mesh: "arkit_mesh.ply", heading_deg_at_start: 123.45)
m = try! ManifestBuilder.finalize(m, in: pkg)
let decoded = try! JSONDecoder().decode(CaptureManifest.self, from: Data(contentsOf: pkg.appendingPathComponent("manifest.json")))
if decoded != m { fail("manifest round trip") }
print("files=\(m.files.count) scan_id=\(id)")
print(pkg.path)
