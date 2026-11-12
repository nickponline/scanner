import ARKit
import ExpoModulesCore
import SceneKit

/// Camera preview bound to the shared ARSession, with an optional wireframe of ARMeshAnchors.
class CapturePreviewView: ExpoView, ARSCNViewDelegate {
  private let sceneView = ARSCNView(frame: .zero)
  private let placeholder = UILabel()
  var showMesh = true {
    didSet { meshRoot.forEach { $0.isHidden = !showMesh } }
  }
  private var meshRoot: [SCNNode] { sceneView.scene.rootNode.childNodes }

  required init(appContext: AppContext? = nil) {
    super.init(appContext: appContext)
    clipsToBounds = true
    backgroundColor = .black
    if CaptureEngine.isSupported {
      sceneView.session = CaptureEngine.shared.session
      sceneView.delegate = self
      sceneView.automaticallyUpdatesLighting = false
      sceneView.preferredFramesPerSecond = 30
      sceneView.rendersCameraGrain = false
      sceneView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
      addSubview(sceneView)
    } else {
      placeholder.text = "Camera preview unavailable\n(no LiDAR / simulator)"
      placeholder.textColor = .lightGray
      placeholder.textAlignment = .center
      placeholder.numberOfLines = 0
      placeholder.autoresizingMask = [.flexibleWidth, .flexibleHeight]
      addSubview(placeholder)
    }
  }

  override func layoutSubviews() {
    super.layoutSubviews()
    sceneView.frame = bounds
    placeholder.frame = bounds
  }

  // MARK: ARSCNViewDelegate (SceneKit render thread)

  func renderer(_ renderer: SCNSceneRenderer, nodeFor anchor: ARAnchor) -> SCNNode? {
    guard let mesh = anchor as? ARMeshAnchor else { return nil }
    let node = SCNNode(geometry: Self.geometry(mesh.geometry))
    node.isHidden = !showMesh
    return node
  }

  func renderer(_ renderer: SCNSceneRenderer, didUpdate node: SCNNode, for anchor: ARAnchor) {
    guard let mesh = anchor as? ARMeshAnchor else { return }
    node.geometry = Self.geometry(mesh.geometry)
    node.isHidden = !showMesh
  }

  private static let material: SCNMaterial = {
    let m = SCNMaterial()
    m.fillMode = .lines
    m.diffuse.contents = UIColor(red: 0.2, green: 0.9, blue: 1.0, alpha: 0.55)
    m.lightingModel = .constant
    m.isDoubleSided = true
    return m
  }()

  static func geometry(_ g: ARMeshGeometry) -> SCNGeometry {
    let v = g.vertices
    let src = SCNGeometrySource(buffer: v.buffer, vertexFormat: v.format, semantic: .vertex,
                                vertexCount: v.count, dataOffset: v.offset, dataStride: v.stride)
    let f = g.faces
    let data = Data(bytes: f.buffer.contents(), count: f.count * f.indexCountPerPrimitive * f.bytesPerIndex)
    let el = SCNGeometryElement(data: data, primitiveType: .triangles, primitiveCount: f.count, bytesPerIndex: f.bytesPerIndex)
    let geo = SCNGeometry(sources: [src], elements: [el])
    geo.firstMaterial = material
    return geo
  }
}
