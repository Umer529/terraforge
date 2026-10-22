import ARKit
import Combine
import RealityKit
import SwiftUI

// Screens from the report's GUI section: Log in, Scan Library, Capture, Review and Upload.

struct LoginView: View {
    @EnvironmentObject var api: APIClient
    @State private var server = UserDefaults.standard.string(forKey: "server") ?? "http://192.168.1.10:8000"
    @State private var username = "operator"
    @State private var password = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        Form {
            Section {
                Text("TerraForge").font(.largeTitle.bold())
            }
            Section("Account") {
                TextField("Server", text: $server).keyboardType(.URL).textInputAutocapitalization(.never)
                TextField("Username or email", text: $username).textInputAutocapitalization(.never)
                SecureField("Password", text: $password)
            }
            if let error { Text(error).foregroundStyle(.red) }
            Button(busy ? "Logging in…" : "Log in") {
                busy = true
                Task {
                    do { try await api.login(server: server, username: username, password: password) }
                    catch { self.error = error.localizedDescription }
                    busy = false
                }
            }.disabled(busy || password.isEmpty)
        }
    }
}

struct LibraryView: View {
    @EnvironmentObject var api: APIClient
    @EnvironmentObject var store: SessionStore
    @State private var capturing = false

    var body: some View {
        NavigationStack {
            List {
                if store.sessions.isEmpty {
                    Text("No scans yet. Tap New scan to start.").foregroundStyle(.secondary)
                }
                ForEach(store.sessions) { s in
                    NavigationLink(value: s) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(s.manifest.title).font(.headline)
                            HStack {
                                Text(s.manifest.capturedAt, style: .date)
                                Text(ByteCountFormatter.string(fromByteCount: s.sizeBytes, countStyle: .file))
                                Spacer()
                                Text(s.state.rawValue.capitalized).font(.caption.bold())
                                    .foregroundStyle(s.state == .uploaded ? .green : .orange)
                            }.font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { idx in idx.map { store.sessions[$0] }.forEach(store.delete) }
            }
            .navigationTitle("Scan library")
            .navigationDestination(for: ScanSession.self) { ReviewView(session: $0) }
            .toolbar {
                ToolbarItem(placement: .bottomBar) {
                    Button { capturing = true } label: { Label("New scan", systemImage: "camera.viewfinder") }
                        .buttonStyle(.borderedProminent)
                        .disabled(!CaptureController.isSupported)
                }
                ToolbarItem(placement: .topBarTrailing) { Button("Log out") { api.logout() } }
            }
            .fullScreenCover(isPresented: $capturing) { CaptureView() }
        }
    }
}

struct ARViewContainer: UIViewRepresentable {
    let arView: ARView
    func makeUIView(context: Context) -> ARView { arView }
    func updateUIView(_ uiView: ARView, context: Context) {}
}

struct CaptureView: View {
    @EnvironmentObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss
    @StateObject private var capture = CaptureController()
    @State private var error: String?

    var body: some View {
        ZStack(alignment: .bottom) {
            ARViewContainer(arView: capture.arView).ignoresSafeArea()
            VStack(spacing: 14) {
                Text(capture.trackingMessage).font(.caption).padding(6).background(.ultraThinMaterial, in: Capsule())
                HStack(spacing: 24) {
                    Label(String(format: "%.0f m", capture.distanceWalked), systemImage: "figure.walk")
                    Label("\(capture.gpsFixCount) GPS", systemImage: "location")
                    Label("\(capture.photoCount) photos", systemImage: "photo.on.rectangle")
                    Label("\(capture.meshAnchorCount) mesh", systemImage: "cube.transparent")
                }
                .font(.subheadline.monospacedDigit()).padding(10).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
                HStack(spacing: 40) {
                    Button("Cancel") { dismiss() }.foregroundStyle(.white)
                    Button(action: toggle) {
                        Circle().fill(capture.isRecording ? .red : .white).frame(width: 74, height: 74)
                            .overlay(Circle().stroke(.white, lineWidth: 4).padding(-6))
                    }
                    Button { capture.takePhoto() } label: { Image(systemName: "camera.fill").font(.title2) }
                        .foregroundStyle(.white).disabled(!capture.isRecording)
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            .padding(.bottom, 30)
        }
    }

    private func toggle() {
        do {
            if capture.isRecording {
                let title = "Scan " + Date().formatted(date: .abbreviated, time: .shortened)
                let manifest = try capture.stop(title: title)
                if let folder = currentFolder { try store.save(manifest: manifest, in: folder) }
                dismiss()
            } else {
                let folder = try store.newSessionFolder()
                currentFolder = folder
                capture.start(in: folder)
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    @State private var currentFolder: URL?
}

struct ReviewView: View {
    @EnvironmentObject var api: APIClient
    @EnvironmentObject var store: SessionStore
    @Environment(\.dismiss) private var dismiss
    let session: ScanSession
    @State private var title = ""
    @State private var notes = ""
    @State private var sites: [Site] = []
    @State private var siteId: Int?
    @State private var progress: (Int, Int)?
    @State private var message: String?
    @AppStorage("wifiOnly") private var wifiOnly = true

    var body: some View {
        Form {
            Section("Scan") {
                TextField("Title", text: $title)
                TextField("Notes", text: $notes, axis: .vertical)
                LabeledContent("Walked", value: String(format: "%.0f m", session.manifest.pathLengthM))
                LabeledContent("GPS fixes", value: "\(session.manifest.gpsFixes)")
                LabeledContent("Photos", value: "\(session.manifest.photos)")
                LabeledContent("Size", value: ByteCountFormatter.string(fromByteCount: session.sizeBytes, countStyle: .file))
                Button("Save") { try? store.rename(session, title: title, notes: notes) }
            }
            Section("Upload") {
                Picker("Site", selection: $siteId) {
                    ForEach(sites) { Text($0.name).tag(Optional($0.id)) }
                }
                Toggle("Upload on Wi-Fi only", isOn: $wifiOnly)
                if case let (done, total)? = progress {
                    ProgressView(value: Double(done), total: Double(total)) { Text("Chunk \(done) of \(total)") }
                }
                Button(session.state == .uploading ? "Resume upload" : "Upload") { Task { await upload() } }
                    .disabled(siteId == nil || progress != nil || session.state == .uploaded)
                if let message { Text(message).font(.footnote) }
            }
            Section {
                Button("Delete scan", role: .destructive) { store.delete(session); dismiss() }
            }
        }
        .navigationTitle(session.manifest.title)
        .task {
            title = session.manifest.title
            notes = session.manifest.notes
            sites = (try? await api.sites()) ?? []
            siteId = siteId ?? sites.first?.id
        }
    }

    private func upload() async {
        guard let siteId else { return }
        try? store.rename(session, title: title, notes: notes)
        do {
            let pkg = try store.package(session)
            progress = (0, 1)
            let scanId = try await api.upload(package: pkg, session: session, siteId: siteId) { done, total in
                Task { @MainActor in progress = (done, total) }
            }
            message = "Uploaded. Scan #\(scanId) is queued for processing."
        } catch {
            message = "Upload paused: \(error.localizedDescription). Tap Resume upload to continue."
        }
        progress = nil
        store.reload()
    }
}
