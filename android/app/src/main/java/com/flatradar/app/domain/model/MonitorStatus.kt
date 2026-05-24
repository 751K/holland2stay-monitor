package com.flatradar.app.domain.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * Public monitor summary — matches iOS MonitorStatus.swift.
 */
@JsonClass(generateAdapter = true)
data class MonitorStatus(
    val total: Int,
    @Json(name = "new_24h") val new24h: Int,
    @Json(name = "new_7d") val new7d: Int,
    @Json(name = "changes_24h") val changes24h: Int,
    @Json(name = "last_scrape") val lastScrape: String
)
