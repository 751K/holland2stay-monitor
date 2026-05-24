package com.flatradar.app.domain.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = true)
data class AdminUserSummary(
    val id: String,
    val name: String,
    val enabled: Boolean,
    @Json(name = "notifications_enabled") val notificationsEnabled: Boolean,
    @Json(name = "channel_count") val channelCount: Int,
    val channels: List<String> = emptyList(),
    @Json(name = "app_login_enabled") val appLoginEnabled: Boolean,
    @Json(name = "has_app_password") val hasAppPassword: Boolean,
    @Json(name = "allow_h2s_login") val allowH2sLogin: Boolean = false,
    @Json(name = "active_devices") val activeDevices: Int,
    @Json(name = "auto_book_enabled") val autoBookEnabled: Boolean,
    @Json(name = "filter_summary") val filterSummary: AdminFilterSummary = AdminFilterSummary()
)

@JsonClass(generateAdapter = true)
data class AdminFilterSummary(
    @Json(name = "max_rent") val maxRent: Double? = null,
    @Json(name = "min_area") val minArea: Double? = null,
    @Json(name = "min_floor") val minFloor: Int? = null,
    val cities: List<String> = emptyList(),
    val energy: String = "",
    @Json(name = "filter_active") val filterActive: Boolean = false
) {
    val compactDescription: String
        get() {
            val parts = mutableListOf<String>()
            maxRent?.let { parts += "≤€${it.toInt()}" }
            minArea?.let { parts += "≥${it.toInt()}m²" }
            minFloor?.let { parts += "F≥$it" }
            if (cities.isNotEmpty()) {
                parts += cities.take(2).joinToString(",") + if (cities.size > 2) "…" else ""
            }
            if (energy.isNotBlank()) parts += "Energy $energy"
            return parts.takeIf { it.isNotEmpty() }?.joinToString(" · ") ?: "—"
        }
}

@JsonClass(generateAdapter = true)
data class AdminUsersResponse(
    val items: List<AdminUserSummary> = emptyList(),
    val total: Int
)

@JsonClass(generateAdapter = true)
data class AdminUserToggleResponse(
    val id: String,
    val enabled: Boolean
)

@JsonClass(generateAdapter = true)
data class AdminUserDeleteResponse(
    val deleted: Boolean,
    val name: String,
    @Json(name = "revoked_sessions") val revokedSessions: Int
)

@JsonClass(generateAdapter = true)
data class AdminMonitorStatus(
    val running: Boolean,
    val pid: Int? = null,
    @Json(name = "last_scrape") val lastScrape: String = "",
    @Json(name = "last_count") val lastCount: String = ""
)

@JsonClass(generateAdapter = true)
data class AdminMonitorActionResponse(
    val started: Boolean? = null,
    val stopped: Boolean? = null,
    val pid: Int? = null,
    val reload: Boolean? = null,
    val method: String? = null
)
