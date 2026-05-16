import Foundation

struct Listing: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let status: String
    let priceRaw: String?
    let priceValue: Double?
    let availableFrom: String?
    let features: [String]
    let featureMap: [String: String]
    let url: String
    let city: String
    let firstSeen: String?
    let lastSeen: String?

    enum CodingKeys: String, CodingKey {
        case id, name, status, features, url, city
        case priceRaw = "price_raw"
        case priceValue = "price_value"
        case availableFrom = "available_from"
        case featureMap = "feature_map"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }
}

extension Listing {
    var isBookable: Bool {
        status.localizedCaseInsensitiveContains("available to book")
    }

    var isLottery: Bool {
        status.localizedCaseInsensitiveContains("lottery")
    }

    var areaText: String? {
        featureValue(matching: ["area", "surface", "living area", "m2", "m²"])
    }

    var floorText: String? {
        featureValue(matching: ["floor", "level"])
    }

    var energyText: String? {
        featureValue(matching: ["energy", "energy label"])
    }

    var contractText: String? {
        featureValue(matching: ["contract", "rental agreement", "agreement"])
    }

    var typeText: String? {
        featureValue(matching: ["type", "property type", "apartment type"])
    }

    var availableDayKey: String? {
        guard let availableFrom, !availableFrom.isEmpty else { return nil }
        return String(availableFrom.prefix(10))
    }

    func featureValue(matching aliases: [String]) -> String? {
        let normalizedAliases = aliases.map(normalizeFeatureKey)
        for (key, value) in featureMap {
            let normalizedKey = normalizeFeatureKey(key)
            if normalizedAliases.contains(where: { normalizedKey.contains($0) || $0.contains(normalizedKey) }) {
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                return trimmed.isEmpty ? nil : trimmed
            }
        }
        return nil
    }

    private func normalizeFeatureKey(_ key: String) -> String {
        key
            .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
    }
}
