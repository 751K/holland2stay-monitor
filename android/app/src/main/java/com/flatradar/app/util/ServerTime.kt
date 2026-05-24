package com.flatradar.app.util

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.time.temporal.ChronoUnit
import java.util.Locale

object ServerTime {
    val zone: ZoneId = ZoneId.of("Europe/Amsterdam")

    private val dateFormats = listOf(
        DateTimeFormatter.ISO_LOCAL_DATE,
        DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss", Locale.US),
        DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm", Locale.US),
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS", Locale.US),
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss", Locale.US),
        DateTimeFormatter.ofPattern("yyyy/MM/dd HH:mm:ss", Locale.US),
        DateTimeFormatter.ofPattern("yyyy/MM/dd HH:mm", Locale.US)
    )

    fun dayKey(raw: String?): String? {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.length < 10) return null
        val day = trimmed.take(10)
        if (day.startsWith("1900") || day.startsWith("2049") || day.startsWith("2050")) return null
        return try {
            LocalDate.parse(day, DateTimeFormatter.ISO_LOCAL_DATE).toString()
        } catch (_: DateTimeParseException) {
            null
        }
    }

    fun parseDate(raw: String?): LocalDate? =
        dayKey(raw)?.let { LocalDate.parse(it, DateTimeFormatter.ISO_LOCAL_DATE) }

    fun parseInstant(raw: String?): Instant? {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.isBlank() || trimmed == "--" || trimmed == "—") return null

        runCatching { return Instant.parse(trimmed) }
        runCatching { return OffsetDateTime.parse(trimmed).toInstant() }

        dateFormats.forEach { formatter ->
            runCatching {
                if (formatter == DateTimeFormatter.ISO_LOCAL_DATE) {
                    LocalDate.parse(trimmed.take(10), formatter).atStartOfDay(zone).toInstant()
                } else {
                    LocalDateTime.parse(trimmed, formatter).atZone(zone).toInstant()
                }
            }.getOrNull()?.let { return it }
        }
        return null
    }

    fun relative(raw: String?): String {
        val instant = parseInstant(raw) ?: return raw?.takeIf { it.isNotBlank() } ?: "—"
        val now = Instant.now()
        val minutes = ChronoUnit.MINUTES.between(instant, now).coerceAtLeast(0)
        return when {
            minutes < 1 -> "now"
            minutes < 60 -> "${minutes}m ago"
            minutes < 60 * 24 -> "${minutes / 60}h ago"
            else -> "${minutes / (60 * 24)}d ago"
        }
    }

    fun shortDay(date: LocalDate): String =
        date.format(DateTimeFormatter.ofPattern("MMM d", Locale.getDefault()))

    fun monthTitle(date: LocalDate): String =
        date.format(DateTimeFormatter.ofPattern("MMMM yyyy", Locale.getDefault()))
}
