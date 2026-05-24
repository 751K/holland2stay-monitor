package com.flatradar.app.domain.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * Chart data for dashboard mini visualizations — matches iOS ChartData.swift.
 */
@JsonClass(generateAdapter = true)
data class ChartData(
    val key: String,
    val days: Int = 30,
    val data: List<ChartEntry>
)

/**
 * Backend chart entries use chart-specific label keys:
 * daily_new -> date, source_dist -> source, status_dist -> status,
 * price/area/floor -> range, hourly_dist -> hour, feature charts -> label.
 */
@JsonClass(generateAdapter = true)
data class ChartEntry(
    @param:Json(name = "label") val rawLabel: String? = null,
    val date: String? = null,
    val city: String? = null,
    val source: String? = null,
    val status: String? = null,
    val range: String? = null,
    val hour: Int? = null,
    val count: Int = 0
) {
    val label: String
        get() = listOfNotNull(rawLabel, date, city, source, status, range, hour?.toString())
            .firstOrNull { it.isNotBlank() }
            ?: "Unknown"
}

fun List<ChartEntry>.bucketed(forKey: String): List<ChartEntry> {
    return when (forKey) {
        "source_dist" -> mergedByBucket { ChartBuckets.sourceBucketLabel(it) }
            .sortedWith(compareBy<ChartEntry> {
                when (it.label) {
                    "H2S" -> 0
                    "OD" -> 1
                    "XR" -> 2
                    else -> 99
                }
            }.thenBy { it.label })
        "type_dist" -> mergedByBucket { ChartBuckets.typeBucketLabel(it) }
        "energy_dist" -> mergedByOrderedBucket(listOf("A+", "A", "B", "C", "D", "E", "F", "G")) {
            ChartBuckets.energyBucketLabel(it)
        }
        else -> this
    }
}

private fun List<ChartEntry>.mergedByBucket(bucket: (String) -> String): List<ChartEntry> {
    val counts = linkedMapOf<String, Int>()
    forEach { entry ->
        val label = bucket(entry.label)
        if (label.isNotBlank()) counts[label] = (counts[label] ?: 0) + entry.count
    }
    return counts.map { (label, count) -> ChartEntry(rawLabel = label, count = count) }
}

private fun List<ChartEntry>.mergedByOrderedBucket(
    orderedKeys: List<String>,
    bucket: (String) -> String
): List<ChartEntry> {
    val counts = mutableMapOf<String, Int>()
    forEach { entry ->
        val label = bucket(entry.label)
        if (label.isNotBlank()) counts[label] = (counts[label] ?: 0) + entry.count
    }
    return orderedKeys.mapNotNull { label ->
        val count = counts[label] ?: return@mapNotNull null
        if (count > 0) ChartEntry(rawLabel = label, count = count) else null
    }
}

object ChartBuckets {
    fun sourceBucketLabel(label: String): String = when (label.lowercase()) {
        "holland2stay" -> "H2S"
        "ourdomain" -> "OD"
        "xior" -> "XR"
        else -> label.uppercase()
    }

    fun typeBucketLabel(label: String): String {
        val trimmed = label.trim()
        if (trimmed.isNotEmpty() && trimmed.all { it.isDigit() }) return "Apt"
        val lower = trimmed.lowercase()
        return when {
            lower.contains("studio") -> "Studio"
            lower.contains("loft") -> "Loft"
            lower.contains("house") -> "House"
            lower.contains("room") || lower.contains("apartment") || lower.startsWith("apt") -> "Apt"
            else -> trimmed.substringBefore("(").trim().ifBlank { trimmed }
        }
    }

    fun energyBucketLabel(label: String): String {
        val cleaned = label.uppercase().trim()
        if (cleaned.startsWith("A+")) return "A+"
        if (cleaned == "A") return "A"
        return listOf("B", "C", "D", "E", "F", "G").firstOrNull { cleaned.startsWith(it) } ?: ""
    }
}
