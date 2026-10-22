import ARKit
import Combine
import CoreImage
import CoreLocation
import Foundation
import ImageIO
import RealityKit

/// Capture module (report Fig. "Modules of the mobile app").
/// ARKit scene reconstruction builds the LiDAR mesh; the camera takes photos
/// automatically while the operator walks, each saved with its exact ARKit pose
/// and intrinsics so the server can project the photos onto the LiDAR geometry
/// (photo + LiDAR fusion). Core Location records GPS fixes paired with the AR
/// camera position so the server can fit the walked path to the GPS track.
@MainActor
final class CaptureController: NSObject, ObservableObject {
    @Published var isRecording = false
    @Published var distanceWalked: Double = 0
    @Published var gpsFixCount = 0
    @Published var meshAnchorCount = 0
    @Published var photoCount = 0
    @Published var trackingMessage = "Move the phone slowly"

    static var isSupported: Bool {
        ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
    }

    /// Auto-capture a photo after this much movement or turning (at most 2 per second).
    static let photoEveryMetres: Float = 0.5
    static let photoEveryDegrees: Float = 20

    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
    private let location = CLLocationManager()
    private let photoQueue = DispatchQueue(label: "terraforge.photos", qos: .utility)
    private let ciContext = CIContext()
    private var fixes: [LocationFix] = []
    private var lastCameraPosition: SIMD3<Float>?
    private var lastPhotoPose: simd_float4x4?
    private var lastPhotoTime: TimeInterval = 0
    private var folder: URL?
    private var startedAt = Date()

    override init() {
        super.init()
        location.delegate = self
        location.desiredAccuracy = kCLLocationAccuracyBest
        location.distanceFilter = 1.0 // one fix per metre walked
        arView.session.delegate = self
        arView.debugOptions.insert(.showSceneUnderstanding) // draw the LiDAR mesh over the camera view
    }

    func start(in folder: URL) {
        self.folder = folder
        fixes = []
        distanceWalked = 0
        gpsFixCount = 0
        photoCount = 0
        lastCameraPosition = nil
        lastPhotoPose = nil
        lastPhotoTime = 0
        startedAt = Date()

        let config = ARWorldTrackingConfiguration()
        config.sceneReconstruction = .mesh
        config.environmentTexturing = .none
        config.worldAlignment = .gravity // y up; heading is recovered from GPS on the server
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            config.frameSemantics.insert(.sceneDepth)
        }
        arView.session.run(config, options: [.resetTracking, .removeExistingAnchors])
        location.requestWhenInUseAuthorization()
        location.startUpdatingLocation()
        isRecording = true
    }

    /// Stop, write mesh.ply + location_fixes.csv into the session folder and return the manifest.
    func stop(title: String) throws -> SessionManifest {
        isRecording = false
        location.stopUpdatingLocation()
        let anchors = arView.session.currentFrame?.anchors.compactMap { $0 as? ARMeshAnchor } ?? []
        arView.session.pause()
        guard let folder else { throw CaptureError.notStarted }

        photoQueue.sync {} // wait until the last photos are on disk
        try MeshExporter.writePLY(anchors: anchors, to: folder.appendingPathComponent("mesh.ply"))
        try writeFixesCSV(to: folder.appendingPathComponent("location_fixes.csv"))

        var manifest = SessionManifest(
            title: title, notes: "", capturedAt: startedAt, deviceModel: Self.deviceModel(),
            pathLengthM: distanceWalked, gpsFixes: fixes.count)
        manifest.photos = photoCount
        return manifest
    }

    /// Manual shutter button: save the current frame now.
    func takePhoto() {
        guard let frame = arView.session.currentFrame else { return }
        savePhoto(frame.capturedImage, pose: frame.camera.transform, intrinsics: frame.camera.intrinsics,
                  resolution: frame.camera.imageResolution, timestamp: frame.timestamp)
    }

    fileprivate func shouldTakePhoto(pose: simd_float4x4, time: TimeInterval) -> Bool {
        guard isRecording, time - lastPhotoTime > 0.5 else { return false }
        guard let last = lastPhotoPose else { return true }
        let moved = simd_distance(SIMD3(last.columns.3.x, last.columns.3.y, last.columns.3.z),
                                  SIMD3(pose.columns.3.x, pose.columns.3.y, pose.columns.3.z))
        let a = -SIMD3(last.columns.2.x, last.columns.2.y, last.columns.2.z) // camera looks down -z
        let b = -SIMD3(pose.columns.2.x, pose.columns.2.y, pose.columns.2.z)
        let turned = acos(min(1, max(-1, simd_dot(simd_normalize(a), simd_normalize(b))))) * 180 / .pi
        return moved > Self.photoEveryMetres || turned > Self.photoEveryDegrees
    }

    /// Save one camera image with the pose and intrinsics the server needs for fusion.
    /// The image stays in the sensor's native (landscape) orientation, which is
    /// the orientation ARKit's intrinsics refer to.
    fileprivate func savePhoto(_ buffer: CVPixelBuffer, pose: simd_float4x4, intrinsics: simd_float3x3,
                               resolution: CGSize, timestamp: TimeInterval) {
        guard let folder else { return }
        photoCount += 1
        lastPhotoPose = pose
        lastPhotoTime = timestamp
        let name = String(format: "photos/img_%04d", photoCount)
        let meta: [String: Any] = [
            "transform_column_major": (0..<4).flatMap { c in (0..<4).map { r in pose[c][r] } },
            "intrinsics": (0..<3).flatMap { c in (0..<3).map { r in intrinsics[c][r] } },
            "width": Int(resolution.width), "height": Int(resolution.height), "timestamp": timestamp,
        ]
        let context = ciContext
        photoQueue.async {
            let image = CIImage(cvPixelBuffer: buffer)
            let options: [CIImageRepresentationOption: Any] = [
                CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.8
            ]
            guard let space = CGColorSpace(name: CGColorSpace.sRGB),
                  let jpeg = context.jpegRepresentation(of: image, colorSpace: space, options: options) else { return }
            try? jpeg.write(to: folder.appendingPathComponent(name + ".jpg"))
            if let json = try? JSONSerialization.data(withJSONObject: meta) {
                try? json.write(to: folder.appendingPathComponent(name + ".json"))
            }
        }
    }

    private func writeFixesCSV(to url: URL) throws {
        let iso = ISO8601DateFormatter()
        var csv = "timestamp,latitude,longitude,altitude,horizontal_accuracy,cam_x,cam_y,cam_z\n"
        for f in fixes {
            csv += "\(iso.string(from: f.timestamp)),\(f.latitude),\(f.longitude),\(f.altitude),"
            csv += "\(f.horizontalAccuracy),\(f.camX),\(f.camY),\(f.camZ)\n"
        }
        try csv.write(to: url, atomically: true, encoding: .utf8)
    }

    private static func deviceModel() -> String {
        var info = utsname()
        uname(&info)
        return withUnsafeBytes(of: &info.machine) { String(decoding: $0.prefix(while: { $0 != 0 }), as: UTF8.self) }
    }
}

enum CaptureError: LocalizedError {
    case notStarted
    var errorDescription: String? { "Capture was not started" }
}

extension CaptureController: ARSessionDelegate {
    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let pose = frame.camera.transform
        let position = SIMD3<Float>(pose.columns.3.x, pose.columns.3.y, pose.columns.3.z)
        let meshCount = frame.anchors.filter { $0 is ARMeshAnchor }.count
        var trackingOK = false
        let message: String
        switch frame.camera.trackingState {
        case .normal: message = "Tracking OK"; trackingOK = true
        case .limited(.excessiveMotion): message = "Slow down"
        case .limited(.insufficientFeatures): message = "Point at more detail"
        case .limited: message = "Initialising…"
        case .notAvailable: message = "Tracking unavailable"
        }
        let buffer = frame.capturedImage
        let intrinsics = frame.camera.intrinsics
        let resolution = frame.camera.imageResolution
        let time = frame.timestamp
        Task { @MainActor in
            if let last = self.lastCameraPosition, self.isRecording {
                let step = simd_distance(SIMD2(last.x, last.z), SIMD2(position.x, position.z))
                if step > 0.05 { self.distanceWalked += Double(step); self.lastCameraPosition = position }
            } else {
                self.lastCameraPosition = position
            }
            // Only photograph with good tracking, so the pose used for fusion is reliable.
            if trackingOK && self.shouldTakePhoto(pose: pose, time: time) {
                self.savePhoto(buffer, pose: pose, intrinsics: intrinsics, resolution: resolution, timestamp: time)
            }
            self.meshAnchorCount = meshCount
            self.trackingMessage = message
        }
    }
}

extension CaptureController: CLLocationManagerDelegate {
    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        Task { @MainActor in
            guard self.isRecording, let cam = self.arView.session.currentFrame?.camera.transform.columns.3 else { return }
            for loc in locations where loc.horizontalAccuracy >= 0 && loc.horizontalAccuracy < 20 {
                self.fixes.append(LocationFix(
                    timestamp: loc.timestamp, latitude: loc.coordinate.latitude, longitude: loc.coordinate.longitude,
                    altitude: loc.altitude, horizontalAccuracy: loc.horizontalAccuracy,
                    camX: cam.x, camY: cam.y, camZ: cam.z))
            }
            self.gpsFixCount = self.fixes.count
        }
    }
}

/// Writes all ARMeshAnchors (vertices in world coordinates + triangles) as a binary PLY.
enum MeshExporter {
    static func writePLY(anchors: [ARMeshAnchor], to url: URL) throws {
        var points: [SIMD3<Float>] = []
        var indices: [UInt32] = []
        for anchor in anchors {
            let first = UInt32(points.count)
            let vertices = anchor.geometry.vertices
            let vbase = vertices.buffer.contents().advanced(by: vertices.offset)
            points.reserveCapacity(points.count + vertices.count)
            for i in 0..<vertices.count {
                let v = vbase.advanced(by: i * vertices.stride).assumingMemoryBound(to: (Float, Float, Float).self).pointee
                let world = anchor.transform * SIMD4<Float>(v.0, v.1, v.2, 1)
                points.append(SIMD3(world.x, world.y, world.z))
            }
            let faces = anchor.geometry.faces
            let fbase = faces.buffer.contents()
            for i in 0..<(faces.count * faces.indexCountPerPrimitive) {
                let local: UInt32 = faces.bytesPerIndex == 4
                    ? fbase.load(fromByteOffset: i * 4, as: UInt32.self)
                    : UInt32(fbase.load(fromByteOffset: i * 2, as: UInt16.self))
                indices.append(first + local)
            }
        }
        var data = Data("""
        ply
        format binary_little_endian 1.0
        comment TerraForge LiDAR mesh (ARKit world frame, y-up)
        element vertex \(points.count)
        property float x
        property float y
        property float z
        element face \(indices.count / 3)
        property list uchar uint vertex_indices
        end_header

        """.utf8)
        data.reserveCapacity(data.count + points.count * 12 + indices.count / 3 * 13)
        for p in points {
            withUnsafeBytes(of: p.x) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.y) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.z) { data.append(contentsOf: $0) }
        }
        for f in stride(from: 0, to: indices.count, by: 3) {
            data.append(3)
            for k in 0..<3 { withUnsafeBytes(of: indices[f + k].littleEndian) { data.append(contentsOf: $0) } }
        }
        try data.write(to: url, options: .atomic)
    }
}
