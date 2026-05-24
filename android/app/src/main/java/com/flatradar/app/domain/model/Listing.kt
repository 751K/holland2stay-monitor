package com.flatradar.app.domain.model

import com.squareup.moshi.JsonClass
import com.squareup.moshi.Json

/**
 * Core listing model — matches iOS Listing.swift.
 */
@JsonClass(generateAdapter = true)
data class Listing(
    val id: String,
    val name: String,
    val status: String,
    val city: String = "",
    val source: String = "holland2stay",
    val url: String = "",
    @Json(name = "price_raw") val priceRaw: String? = null,
    @Json(name = "price_value") val priceValue: Float? = null,
    @Json(name = "area_text") val areaText: String? = null,
    @Json(name = "area_value") val areaValue: Int? = null,
    @Json(name = "energy_label") val energyLabel: String? = null,
    @Json(name = "building_text") val buildingText: String? = null,
    val finishing: String? = null,
    val floor: String? = null,
    val rooms: String? = null,
    val occupancy: String? = null,
    @Json(name = "contract_type") val contractType: String? = null,
    @Json(name = "tenant_requirement") val tenantRequirement: String? = null,
    @Json(name = "available_from") val availableFrom: String? = null,
    @Json(name = "available_from_raw") val availableFromRaw: String? = null,
    @Json(name = "first_seen") val firstSeen: String? = null,
    @Json(name = "first_seen_raw") val firstSeenRaw: String? = null,
    @Json(name = "last_seen") val lastSeen: String? = null,
    val features: List<String> = emptyList(),
    @Json(name = "feature_map") val featureMap: Map<String, String>? = null,
    val latitude: Double? = null,
    val longitude: Double? = null
) {
    /** Exposed part of Listing data for listing rows — keeps ListAdapter diff checks cheap. */
    val statusKind: StatusKind
        get() = when {
            status.lowercase().contains("available to book") -> StatusKind.BOOK
            status.lowercase().contains("lottery") -> StatusKind.LOTTERY
            status.lowercase().contains("reserved") || status.lowercase().contains("rented") -> StatusKind.RESERVED
            else -> StatusKind.OTHER
        }

    val displayPrice: String
        get() = priceRaw ?: "—"

    val displayArea: String
        get() = areaText.cleanValue()
            ?: featureValue("area", "surface", "living area", "m2", "m²")
            ?: "—"

    val displayCity: String
        get() = city.ifEmpty { "—" }

    val displayAvailableFrom: String
        get() {
            val raw = availableFrom ?: return "—"
            return if (raw.length == 10) raw.takeLast(6) else raw
        }

    val displayType: String
        get() = featureValue("type", "property type", "apartment type") ?: "—"

    val displayBuilding: String
        get() = buildingText.cleanValue()
            ?: featureValue("building", "building name", "building_name", "complex")
            ?: "—"

    val displayFloor: String
        get() = floor.cleanValue() ?: featureValue("floor", "level") ?: "—"

    val displayRooms: String
        get() = rooms.cleanValue() ?: featureValue("rooms", "bedrooms", "bedroom") ?: "—"

    val displayEnergy: String
        get() = energyLabel.cleanValue() ?: featureValue("energy", "energy label") ?: "—"

    val displayFinishing: String
        get() = finishing.cleanValue() ?: featureValue("finishing", "furnished", "furniture") ?: "—"

    val displayOccupancy: String
        get() = occupancy.cleanValue() ?: featureValue("occupancy", "suitable for", "persons", "person") ?: "—"

    val displayContract: String
        get() = contractType.cleanValue()
            ?: featureValue("contract", "rental agreement", "agreement")
            ?: "—"

    val displayTenant: String
        get() = tenantRequirement.cleanValue()
            ?: featureValue("tenant", "tenant requirement", "requirements", "target group")
            ?: "—"

    fun hasFeatureKeyMatching(vararg keys: String): Boolean =
        featureMap.orEmpty().keys.any { key ->
            val normalized = key.normalizedFeatureKey()
            keys.any { normalized.contains(it.normalizedFeatureKey()) }
        }

    private fun featureValue(vararg keys: String): String? {
        val direct = featureMap.orEmpty().entries.firstNotNullOfOrNull { (key, value) ->
            val normalized = key.normalizedFeatureKey()
            if (keys.any { normalized.contains(it.normalizedFeatureKey()) }) {
                value.cleanValue()
            } else {
                null
            }
        }
        if (direct != null) return direct

        return features.firstNotNullOfOrNull { raw ->
            val parts = raw.split(":", limit = 2)
            if (parts.size != 2) return@firstNotNullOfOrNull null
            val normalized = parts[0].normalizedFeatureKey()
            if (keys.any { normalized.contains(it.normalizedFeatureKey()) }) {
                parts[1].cleanValue()
            } else {
                null
            }
        }
    }
}

enum class StatusKind {
    BOOK, LOTTERY, RESERVED, OTHER
}

private fun String?.cleanValue(): String? =
    this?.trim()?.takeIf { it.isNotBlank() }

private fun String.normalizedFeatureKey(): String =
    lowercase()
        .replace("_", " ")
        .replace("-", " ")
        .replace(Regex("\\s+"), " ")
        .trim()
