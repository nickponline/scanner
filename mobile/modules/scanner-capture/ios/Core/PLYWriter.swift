import Foundation

/// Binary little-endian PLY: `vertex {float x,y,z}`, `face {list uchar uint vertex_indices}`.
public enum PLYWriter {
  public struct Part {
    public var vertices: [Float]   // xyz triples, already in the output frame
    public var faces: [UInt32]     // index triples, local to this part
    public init(vertices: [Float], faces: [UInt32]) { self.vertices = vertices; self.faces = faces }
  }

  public static func write(_ parts: [Part], to url: URL, comment: String = "ARKit world frame (Y up, metres)") throws {
    let nv = parts.reduce(0) { $0 + $1.vertices.count / 3 }
    let nf = parts.reduce(0) { $0 + $1.faces.count / 3 }
    let header = """
    ply
    format binary_little_endian 1.0
    comment \(comment)
    element vertex \(nv)
    property float x
    property float y
    property float z
    element face \(nf)
    property list uchar uint vertex_indices
    end_header\n
    """
    FileManager.default.createFile(atPath: url.path, contents: nil)
    let fh = try FileHandle(forWritingTo: url)
    defer { try? fh.close() }
    try fh.write(contentsOf: Data(header.utf8))
    for p in parts {
      var d = Data(capacity: p.vertices.count * 4)
      for v in p.vertices { DepthRecord.appendLE(&d, v.bitPattern) }
      try fh.write(contentsOf: d)
    }
    var base: UInt32 = 0
    for p in parts {
      var d = Data(capacity: (p.faces.count / 3) * 13)
      var i = 0
      while i + 2 < p.faces.count {
        d.append(3)
        DepthRecord.appendLE(&d, p.faces[i] + base)
        DepthRecord.appendLE(&d, p.faces[i + 1] + base)
        DepthRecord.appendLE(&d, p.faces[i + 2] + base)
        i += 3
      }
      try fh.write(contentsOf: d)
      base += UInt32(p.vertices.count / 3)
    }
  }
}
