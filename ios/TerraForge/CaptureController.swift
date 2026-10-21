import ARKit
import CoreLocation
import Foundation
import RealityKit

/// Capture module (report Fig. "Modules of the mobile app").
/// ARKit scene reconstruction builds the LiDAR mesh; Core Location records GPS
/// fixes, each paired with the AR camera position so the server can fit the
/// walked path to the GPS track. Photos are saved with their camera pose.
@MainActor
final class CaptureController: NSObject, ObservableObject {
    @Published var isRecording = false
    @Published var distanceWalked: Double = 0
    @Published var gpsFixCount = 0
    @Published var meshAnchorCount = 0
    @Published var trackingMessage = "Move the phone slowly"

    static var isSupported: Bool {
        ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
    }

    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
    private let location = CLLocationManager()
    private var fixes: [LocationFix] = []
    private var lastCameraPosition: SIMD3<Float>?
    private var folder: URL?
    private var startedAt = Date()
    private var photoCount = 0

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

        try MeshExporter.writePLY(anchors: anchors, to: folder.appendingPathComponent("mesh.ply"))
        try writeFixesCSV(to: folder.appendingPathComponent("location_fixes.csv"))

        return SessionManifest(
            title: title, notes: "", capturedAt: startedAt, deviceModel: Self.deviceModel(),
            pathLengthM: distanceWalked, gpsFixes: fixes.count)
    }

    /// Save the current camera image with its pose (used later for photogrammetry).
    func takePhoto() {
        guard let frame = arView.session.currentFrame, let folder else { return }
        let image = CIImage(cvPixelBuffer: frame.capturedImage)
        guard let jpeg = CIContext().jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB()) else { return }
        photoCount += 1
        let name = String(format: "photos/img_%04d", photoCount)
        try? jpeg.write(to: folder.appendingPathComponent(name + ".jpg"))
        let pose = (0..<4).flatMap { c in (0..<4).map { r in frame.camera.transform[c][r] } }
        let meta: [String: Any] = ["transform_column_major": pose,
                                   "intrinsics": (0..<3).flatMap { c in (0..<3).map { r in frame.camera.intrinsics[c][r] } },
                                   "timestamp": frame.timestamp]
        if let json = try? JSONSerialization.data(withJSONObject: meta) {
            try? json.write(to: folder.appendingPathComponent(name + ".json"))
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
        let t = frame.camera.transform.columns.3
        let position = SIMD3<Float>(t.x, t.y, t.z)
        let meshCount = frame.anchors.filter { $0 is ARMeshAnchor }.count
        let message: String
        switch frame.camera.trackingState {
        case .normal: message = "Tracking OK"
        case .limited(.excessiveMotion): message = "Slow down"
        case .limited(.insufficientFeatures): message = "Point at more detail"
        case .limited: message = "Initialising…"
        case .notAvailable: message = "Tracking unavailable"
        }
        Task { @MainActor in
            if let last = self.lastCameraPosition, self.isRecording {
                let step = simd_distance(SIMD2(last.x, last.z), SIMD2(position.x, position.z))
                if step > 0.05 { self.distanceWalked += Double(step); self.lastCameraPosition = position }
            } else {
                self.lastCameraPosition = position
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

/// Writes the vertices of all ARMeshAnchors, in world coordinates, as a binary PLY.
enum MeshExporter {
    static func writePLY(anchors: [ARMeshAnchor], to url: URL) throws {
        var points: [SIMD3<Float>] = []
        for anchor in anchors {
            let source = anchor.geometry.vertices
            let base = source.buffer.contents().advanced(by: source.offset)
            points.reserveCapacity(points.count + source.count)
            for i in 0..<source.count {
                let v = base.advanced(by: i * source.stride).assumingMemoryBound(to: (Float, Float, Float).self).pointee
                let world = anchor.transform * SIMD4<Float>(v.0, v.1, v.2, 1)
                points.append(SIMD3(world.x, world.y, world.z))
            }
        }
        var data = Data("""
        ply
        format binary_little_endian 1.0
        comment TerraForge capture (ARKit world frame, y-up)
        element vertex \(points.count)
        property float x
        property float y
        property float z
        end_header

        """.utf8)
        points.withUnsafeBufferPointer { buf in
            for p in buf {
                withUnsafeBytes(of: p.x) { data.append(contentsOf: $0) }
                withUnsafeBytes(of: p.y) { data.append(contentsOf: $0) }
                withUnsafeBytes(of: p.z) { data.append(contentsOf: $0) }
            }
        }
        try data.write(to: url, options: .atomic)
    }
}
