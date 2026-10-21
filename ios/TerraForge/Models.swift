import Foundation

/// One GPS reading paired with the AR camera position at that moment.
/// The server fits the camera path to the GPS track to georeference the scan.
struct LocationFix: Codable {
    var timestamp: Date
    var latitude: Double
    var longitude: Double
    var altitude: Double
    var horizontalAccuracy: Double
    var camX: Float
    var camY: Float
    var camZ: Float
}

/// manifest.json inside a scan package (format "terraforge-session/1").
struct SessionManifest: Codable {
    var format = "terraforge-session/1"
    var title: String
    var notes: String
    var capturedAt: Date
    var deviceModel: String
    var upAxis = "y"
    var scanFile = "mesh.ply"
    var pathLengthM: Double
    var gpsFixes: Int
    var photos = 0
    var siteId: Int?

    enum CodingKeys: String, CodingKey {
        case format, title, notes
        case capturedAt = "captured_at"
        case deviceModel = "device_model"
        case upAxis = "up_axis"
        case scanFile = "scan_file"
        case pathLengthM = "path_length_m"
        case gpsFixes = "gps_fixes"
        case photos
        case siteId = "site_id"
    }
}

/// Server-side upload session, saved next to the scan so an interrupted
/// upload resumes from the last confirmed chunk after an app restart.
struct UploadRecord: Codable {
    var uploadId: String
    var scanId: Int
    var chunkSize: Int
    var totalChunks: Int

    enum CodingKeys: String, CodingKey {
        case uploadId = "upload_id"
        case scanId = "scan_id"
        case chunkSize = "chunk_size"
        case totalChunks = "total_chunks"
    }
}

struct Site: Codable, Identifiable, Hashable {
    var id: Int
    var name: String
}

enum SessionState: String, Codable {
    case saved, uploading, uploaded
}
