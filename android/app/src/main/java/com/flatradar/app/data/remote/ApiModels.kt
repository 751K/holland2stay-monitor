package com.flatradar.app.data.remote

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import com.flatradar.app.domain.model.Listing

/**
 * Generic API response envelope — matches iOS APIResponse<T>.
 * Every /api/v1/ endpoint returns {ok, data, error}.
 */
@JsonClass(generateAdapter = true)
data class ApiResponse<T>(
    val ok: Boolean,
    val data: T?,
    val error: ApiError?
)

@JsonClass(generateAdapter = true)
data class ApiError(
    val code: String,
    val message: String
)

// ── Paginated responses ──────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class ListingsResponse(
    val items: List<com.flatradar.app.domain.model.Listing>,
    val total: Int,
    val limit: Int,
    val offset: Int,
    val filtered: Boolean? = null
)

@JsonClass(generateAdapter = true)
data class NotificationsResponse(
    val items: List<com.flatradar.app.domain.model.NotificationItem>,
    val total: Int,
    val unread: Int,
    val limit: Int,
    val offset: Int
)

// ── Me summary ────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class MeSummaryResponse(
    val role: String,
    @Json(name = "total_in_db") val totalInDb: Int,
    @Json(name = "new_24h_total") val new24hTotal: Int,
    @Json(name = "matched_total") val matchedTotal: Int,
    @Json(name = "matched_available") val matchedAvailable: Int?,
    @Json(name = "last_scrape") val lastScrape: String,
    @Json(name = "filter_active") val filterActive: Boolean
)

// ── Filter options ────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class FilterOptionsResponse(
    val cities: List<String> = emptyList(),
    val sources: List<String> = emptyList(),
    val occupancy: List<String> = emptyList(),
    val types: List<String> = emptyList(),
    val neighborhoods: List<String> = emptyList(),
    val contract: List<String> = emptyList(),
    val tenant: List<String> = emptyList(),
    val offer: List<String> = emptyList(),
    val finishing: List<String> = emptyList(),
    val energy: List<String> = emptyList()
)

// ── Charts ────────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class ChartKeysResponse(
    val charts: List<String>
)

// ── Map / Calendar ────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class MapResponse(
    val listings: List<MapCalendarListingDto> = emptyList(),
    val uncached: Int = 0
) {
    fun allListings(): List<Listing> = listings.map { it.toListing() }
}

@JsonClass(generateAdapter = true)
data class CalendarResponse(
    val listings: List<MapCalendarListingDto> = emptyList()
) {
    fun allListings(): List<Listing> = listings.map { it.toListing() }
}

@JsonClass(generateAdapter = true)
data class MapCalendarListingDto(
    val id: String,
    val name: String,
    val status: String,
    val source: String? = null,
    val url: String? = null,
    val city: String? = null,
    val address: String? = null,
    @Json(name = "price_raw") val priceRaw: String? = null,
    @Json(name = "price_value") val priceValue: Float? = null,
    @Json(name = "area_text") val areaText: String? = null,
    @Json(name = "area_value") val areaValue: Int? = null,
    @Json(name = "available_from") val availableFrom: String? = null,
    @Json(name = "available_from_raw") val availableFromRaw: String? = null,
    @Json(name = "building") val building: String? = null,
    @Json(name = "building_text") val buildingText: String? = null,
    @Json(name = "energy_label") val energyLabel: String? = null,
    val finishing: String? = null,
    val floor: String? = null,
    val rooms: String? = null,
    val occupancy: String? = null,
    @Json(name = "contract_type") val contractType: String? = null,
    @Json(name = "tenant_requirement") val tenantRequirement: String? = null,
    @Json(name = "first_seen") val firstSeen: String? = null,
    @Json(name = "first_seen_raw") val firstSeenRaw: String? = null,
    @Json(name = "last_seen") val lastSeen: String? = null,
    @Json(name = "feature_map") val featureMap: Map<String, String>? = null,
    @Json(name = "lat") val lat: Double? = null,
    @Json(name = "lng") val lng: Double? = null,
    val latitude: Double? = null,
    val longitude: Double? = null
) {
    fun toListing(): Listing {
        // Merge DTO flat fields into featureMap — Listing derives everything from featureMap.
        val mergedFeatures = (featureMap ?: emptyMap()).toMutableMap()
        areaText?.let { mergedFeatures.putIfAbsent("area", it) }
        energyLabel?.let { mergedFeatures.putIfAbsent("energy label", it) }
        (buildingText ?: building ?: address)?.let { mergedFeatures.putIfAbsent("building", it) }
        finishing?.let { mergedFeatures.putIfAbsent("finishing", it) }
        floor?.let { mergedFeatures.putIfAbsent("floor", it) }
        rooms?.let { mergedFeatures.putIfAbsent("rooms", it) }
        occupancy?.let { mergedFeatures.putIfAbsent("occupancy", it) }
        contractType?.let { mergedFeatures.putIfAbsent("contract type", it) }
        tenantRequirement?.let { mergedFeatures.putIfAbsent("tenant requirement", it) }

        return Listing(
            id = id, name = name, status = status,
            city = city.orEmpty(), source = source ?: "holland2stay", url = url.orEmpty(),
            priceRaw = priceRaw, priceValue = priceValue,
            availableFrom = availableFrom, availableFromRaw = availableFromRaw,
            firstSeen = firstSeen, firstSeenRaw = firstSeenRaw, lastSeen = lastSeen,
            featureMap = mergedFeatures,
            latitude = latitude ?: lat, longitude = longitude ?: lng,
        )
    }
}

// ── Mark read ─────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class MarkReadRequest(
    val ids: List<Int>? = null
)

@JsonClass(generateAdapter = true)
data class MarkReadResponse(
    val marked: Boolean
)

// ── Me filter ─────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class MeFilterResponse(
    val role: String,
    val filter: com.flatradar.app.domain.model.ListingFilter,
    @Json(name = "is_empty") val isEmpty: Boolean
)

// ── Account ───────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class ChangePasswordRequest(
    @Json(name = "current_password") val currentPassword: String,
    @Json(name = "new_password") val newPassword: String
)

@JsonClass(generateAdapter = true)
data class ChangePasswordResponse(
    @Json(name = "revoked_other_sessions") val revokedOtherSessions: Int
)

@JsonClass(generateAdapter = true)
data class AccountDeleteResponse(
    val deleted: Boolean,
    @Json(name = "user_id") val userId: String
)

@JsonClass(generateAdapter = true)
data class RevokeResponse(
    val revoked: Boolean
)

// ── Devices ───────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class DeviceRegisterRequest(
    @Json(name = "device_token") val deviceToken: String,
    val env: String,
    val platform: String = "android",
    val model: String,
    @Json(name = "bundle_id") val bundleId: String,
    val language: String
)

@JsonClass(generateAdapter = true)
data class DeviceRegisterResponse(
    @Json(name = "device_id") val deviceId: Int,
    val env: String,
    val platform: String
)

@JsonClass(generateAdapter = true)
data class DevicesResponse(
    val items: List<DeviceInfo> = emptyList()
)

@JsonClass(generateAdapter = true)
data class DeviceInfo(
    val id: Int,
    @Json(name = "device_token_hint") val deviceTokenHint: String,
    val env: String,
    val platform: String,
    val model: String,
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "last_seen") val lastSeen: String,
    val disabled: Boolean,
    @Json(name = "disabled_reason") val disabledReason: String
)

@JsonClass(generateAdapter = true)
data class DeviceDeleteResponse(
    val deleted: Boolean
)

@JsonClass(generateAdapter = true)
data class DeviceTestRequest(
    val title: String,
    val body: String,
    @Json(name = "apns_only") val apnsOnly: Boolean = false,
    @Json(name = "notification_only") val notificationOnly: Boolean = false
)

@JsonClass(generateAdapter = true)
data class DeviceTestResponse(
    val sent: Int,
    val total: Int,
    val results: List<DeviceTestResult> = emptyList(),
    @Json(name = "notification_id") val notificationId: Int? = null
)

@JsonClass(generateAdapter = true)
data class DeviceTestResult(
    @Json(name = "device_token_hint") val deviceTokenHint: String,
    val env: String,
    val status: Int? = null,
    val reason: String = "",
    val ok: Boolean
)

// ── Feedback ──────────────────────────────────────────────────────

@JsonClass(generateAdapter = true)
data class LegalResponse(
    val terms: String,
    val privacy: String,
    @Json(name = "updated_at") val updatedAt: String
)

@JsonClass(generateAdapter = true)
data class FeedbackRequest(
    val kind: String,
    val message: String,
    @Json(name = "user_name") val userName: String = "",
    @Json(name = "app_version") val appVersion: String = ""
)

@JsonClass(generateAdapter = true)
data class FeedbackResponse(
    val submitted: Boolean
)
