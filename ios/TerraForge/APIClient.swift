import CryptoKit
import Foundation

/// REST client for the TerraForge backend (token auth, chunked resumable upload).
@MainActor
final class APIClient: ObservableObject {
    @Published var serverURL: URL = URL(string: UserDefaults.standard.string(forKey: "server") ?? "http://192.168.1.10:8000")!
    @Published private(set) var token: String? = UserDefaults.standard.string(forKey: "token")
    @Published private(set) var username: String? = UserDefaults.standard.string(forKey: "username")

    var isLoggedIn: Bool { token != nil }

    struct APIError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    // MARK: auth & sites

    func login(server: String, username: String, password: String) async throws {
        guard let url = URL(string: server) else { throw APIError(message: "Invalid server address") }
        serverURL = url
        struct Reply: Decodable { let token: String; let username: String }
        let reply: Reply = try await send("POST", "/api/auth/login", json: ["username": username, "password": password], auth: false)
        token = reply.token
        self.username = reply.username
        UserDefaults.standard.set(server, forKey: "server")
        UserDefaults.standard.set(reply.token, forKey: "token")
        UserDefaults.standard.set(reply.username, forKey: "username")
    }

    func logout() {
        token = nil
        username = nil
        UserDefaults.standard.removeObject(forKey: "token")
    }

    func sites() async throws -> [Site] {
        struct Reply: Decodable { let sites: [Site] }
        let reply: Reply = try await send("GET", "/api/sites")
        return reply.sites
    }

    // MARK: chunked upload

    /// Uploads a zipped session in SHA-256-checked chunks. Progress is saved to
    /// upload.json in the session folder, so after a failure (or an app
    /// restart) the next call asks the server which chunks it has and resumes.
    func upload(package: URL, session: ScanSession, siteId: Int,
                progress: @escaping (Int, Int) -> Void) async throws -> Int {
        let recordURL = session.folder.appendingPathComponent("upload.json")
        let handle = try FileHandle(forReadingFrom: package)
        defer { try? handle.close() }
        let size = try FileManager.default.attributesOfItem(atPath: package.path)[.size] as! Int

        var record: UploadRecord
        if let data = try? Data(contentsOf: recordURL), let saved = try? JSONDecoder().decode(UploadRecord.self, from: data) {
            record = saved
        } else {
            let body: [String: Any] = [
                "site_id": siteId, "filename": package.lastPathComponent, "size": size,
                "chunk_size": 2 * 1024 * 1024, "sha256": try Self.sha256(of: package),
                "title": session.manifest.title, "notes": session.manifest.notes,
                "device_model": session.manifest.deviceModel,
                "captured_at": ISO8601DateFormatter().string(from: session.manifest.capturedAt),
            ]
            record = try await send("POST", "/api/uploads", json: body)
            try JSONEncoder().encode(record).write(to: recordURL)
        }

        struct Status: Decodable { let received: [Int] }
        let status: Status = try await send("GET", "/api/uploads/\(record.uploadId)")
        var received = Set(status.received)
        progress(received.count, record.totalChunks)

        for index in 0..<record.totalChunks where !received.contains(index) {
            try handle.seek(toOffset: UInt64(index * record.chunkSize))
            let chunk = try handle.read(upToCount: record.chunkSize) ?? Data()
            try await sendChunk(chunk, index: index, uploadId: record.uploadId)
            received.insert(index)
            progress(received.count, record.totalChunks)
        }

        struct Done: Decodable { let scan_id: Int }
        let done: Done = try await send("POST", "/api/uploads/\(record.uploadId)/complete", json: [:])
        try? FileManager.default.removeItem(at: recordURL)
        FileManager.default.createFile(atPath: session.folder.appendingPathComponent("uploaded").path, contents: nil)
        return done.scan_id
    }

    private func sendChunk(_ chunk: Data, index: Int, uploadId: String) async throws {
        let digest = SHA256.hash(data: chunk).map { String(format: "%02x", $0) }.joined()
        var lastError: Error?
        for attempt in 0..<4 {
            var req = request("PUT", "/api/uploads/\(uploadId)/chunks/\(index)")
            req.setValue(digest, forHTTPHeaderField: "X-Chunk-SHA256")
            req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            // "Upload on Wi-Fi only" (packages can be ~1 GB)
            req.allowsCellularAccess = !UserDefaults.standard.bool(forKey: "wifiOnly")
            do {
                let (_, resp) = try await URLSession.shared.upload(for: req, from: chunk)
                if (resp as? HTTPURLResponse)?.statusCode == 200 { return }
                lastError = APIError(message: "Chunk \(index) rejected")
            } catch {
                lastError = error
            }
            try await Task.sleep(nanoseconds: UInt64(attempt + 1) * 800_000_000)
        }
        throw lastError ?? APIError(message: "Chunk \(index) failed")
    }

    // MARK: plumbing

    private func request(_ method: String, _ path: String, auth: Bool = true) -> URLRequest {
        var req = URLRequest(url: serverURL.appendingPathComponent(path))
        req.httpMethod = method
        if auth, let token { req.setValue("Token \(token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func send<T: Decodable>(_ method: String, _ path: String, json: [String: Any]? = nil, auth: Bool = true) async throws -> T {
        var req = request(method, path, auth: auth)
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(message: msg ?? "Server error")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let block = try handle.read(upToCount: 1 << 20), !block.isEmpty { hasher.update(data: block) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
