package com.flatradar.app.domain.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * Auth-related DTOs — matches iOS AuthModels.swift.
 */

@JsonClass(generateAdapter = true)
data class LoginRequest(
    val username: String,
    val password: String,
    @Json(name = "device_name") val deviceName: String,
    @Json(name = "ttl_days") val ttlDays: Int = 90
)

@JsonClass(generateAdapter = true)
data class LoginResponse(
    val token: String,
    @Json(name = "token_id") val tokenId: Int? = null,
    val role: String,
    @Json(name = "user_id") val userId: String? = null,
    @Json(name = "device_name") val deviceName: String? = null,
    @Json(name = "ttl_days") val ttlDays: Int? = null,
    val user: UserInfo?
)

@JsonClass(generateAdapter = true)
data class MeResponse(
    val role: String,
    val user: UserInfo?
)

@JsonClass(generateAdapter = true)
data class UserInfo(
    val id: String,
    val name: String,
    val role: String? = null,
    val enabled: Boolean = true,
    @Json(name = "notifications_enabled") val notificationsEnabled: Boolean = false,
    @Json(name = "listing_filter") val listingFilter: ListingFilter? = null
)
