package com.flatradar.app.domain.model

import com.squareup.moshi.JsonClass

/**
 * User listing filter — matches iOS ListingFilter.swift.
 */
@JsonClass(generateAdapter = true)
data class ListingFilter(
    val cities: List<String> = emptyList(),
    val sources: List<String> = emptyList(),
    val types: List<String> = emptyList(),
    val occupancy: List<String> = emptyList(),
    val neighborhoods: List<String> = emptyList(),
    val contract: List<String> = emptyList(),
    val tenant: List<String> = emptyList(),
    val offer: List<String> = emptyList(),
    val finishing: List<String> = emptyList(),
    val energy: List<String> = emptyList(),
    @com.squareup.moshi.Json(name = "min_rent") val minRent: Int? = null,
    @com.squareup.moshi.Json(name = "max_rent") val maxRent: Int? = null,
    @com.squareup.moshi.Json(name = "min_area") val minArea: Int? = null,
    @com.squareup.moshi.Json(name = "max_area") val maxArea: Int? = null,
    @com.squareup.moshi.Json(name = "min_floor") val minFloor: Int? = null,
    @com.squareup.moshi.Json(name = "max_floor") val maxFloor: Int? = null
) {
    fun isEmpty(): Boolean = cities.isEmpty() && sources.isEmpty() && types.isEmpty() &&
        occupancy.isEmpty() && neighborhoods.isEmpty() && contract.isEmpty() &&
        tenant.isEmpty() && offer.isEmpty() && finishing.isEmpty() && energy.isEmpty() &&
        minRent == null && maxRent == null && minArea == null && maxArea == null &&
        minFloor == null && maxFloor == null
}
