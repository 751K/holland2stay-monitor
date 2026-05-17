import Foundation

enum AppVersion {
    static var short: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
    }

    static var build: String {
        Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? ""
    }

    static var displayName: String {
        build.isEmpty ? "FlatRadar v\(short)" : "FlatRadar v\(short) (\(build))"
    }
}
