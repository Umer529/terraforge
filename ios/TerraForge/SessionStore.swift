import Combine
import Foundation

/// A capture kept on the phone: Documents/Sessions/<id>/
///   manifest.json, location_fixes.csv, mesh.ply, photos/, upload.json (while uploading)
struct ScanSession: Identifiable, Hashable {
    let id: String
    let folder: URL
    var manifest: SessionManifest
    var state: SessionState
    var sizeBytes: Int64

    static func == (a: ScanSession, b: ScanSession) -> Bool { a.id == b.id }
    func hash(into h: inout Hasher) { h.combine(id) }
}

/// Offline-first local storage (report Ch. 5, "Offline First Capture").
/// Everything is written to disk during capture so a scan survives the app
/// closing or the phone restarting, and no network is needed until upload.
@MainActor
final class SessionStore: ObservableObject {
    @Published private(set) var sessions: [ScanSession] = []

    let root: URL = {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let dir = docs.appendingPathComponent("Sessions", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        e.outputFormatting = [.prettyPrinted, .sortedKeys]
        return e
    }()
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    init() { reload() }

    func newSessionFolder() throws -> URL {
        let folder = root.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("photos"), withIntermediateDirectories: true)
        return folder
    }

    func reload() {
        let fm = FileManager.default
        let folders = (try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        sessions = folders.compactMap { folder in
            guard let data = try? Data(contentsOf: folder.appendingPathComponent("manifest.json")),
                  let manifest = try? Self.decoder.decode(SessionManifest.self, from: data) else { return nil }
            let state: SessionState
            if fm.fileExists(atPath: folder.appendingPathComponent("uploaded").path) { state = .uploaded }
            else if fm.fileExists(atPath: folder.appendingPathComponent("upload.json").path) { state = .uploading }
            else { state = .saved }
            return ScanSession(id: folder.lastPathComponent, folder: folder, manifest: manifest,
                               state: state, sizeBytes: Self.folderSize(folder))
        }
        .sorted { $0.manifest.capturedAt > $1.manifest.capturedAt }
    }

    func save(manifest: SessionManifest, in folder: URL) throws {
        try Self.encoder.encode(manifest).write(to: folder.appendingPathComponent("manifest.json"), options: .atomic)
        reload()
    }

    func rename(_ session: ScanSession, title: String, notes: String) throws {
        var m = session.manifest
        m.title = title
        m.notes = notes
        try save(manifest: m, in: session.folder)
    }

    func delete(_ session: ScanSession) {
        try? FileManager.default.removeItem(at: session.folder)
        reload()
    }

    /// Zip the session folder for upload. NSFileCoordinator's .forUploading
    /// option produces a zip archive of a directory without third-party code.
    func package(_ session: ScanSession) throws -> URL {
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent("\(session.id).zip")
        try? FileManager.default.removeItem(at: dest)
        var coordError: NSError?
        var copyError: Error?
        NSFileCoordinator().coordinate(readingItemAt: session.folder, options: [.forUploading], error: &coordError) { zipURL in
            do { try FileManager.default.copyItem(at: zipURL, to: dest) } catch { copyError = error }
        }
        if let coordError { throw coordError }
        if let copyError { throw copyError }
        return dest
    }

    private static func folderSize(_ url: URL) -> Int64 {
        let keys: [URLResourceKey] = [.fileSizeKey]
        let files = FileManager.default.enumerator(at: url, includingPropertiesForKeys: keys)
        var total: Int64 = 0
        while let f = files?.nextObject() as? URL {
            total += Int64((try? f.resourceValues(forKeys: Set(keys)).fileSize) ?? 0)
        }
        return total
    }
}
