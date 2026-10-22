import Combine
import SwiftUI

@main
struct TerraForgeApp: App {
    @StateObject private var api = APIClient()
    @StateObject private var store = SessionStore()

    var body: some Scene {
        WindowGroup {
            Group {
                if api.isLoggedIn { LibraryView() } else { LoginView() }
            }
            .environmentObject(api)
            .environmentObject(store)
        }
    }
}
