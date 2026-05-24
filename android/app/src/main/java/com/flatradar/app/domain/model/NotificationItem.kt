package com.flatradar.app.domain.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/**
 * Notification model — matches iOS NotificationItem.swift.
 */
@JsonClass(generateAdapter = true)
data class NotificationItem(
    val id: Int,
    @Json(name = "created_at") val createdAt: String,
    val type: String,
    val title: String,
    val body: String,
    val url: String,
    @Json(name = "listing_id") val listingId: String,
    val read: Int
) {
    val isRead: Boolean get() = read != 0

    val listingTitleHint: String
        get() {
            val separators = listOf("：", ":")
            var value = title
            for (sep in separators) {
                val idx = value.indexOf(sep)
                if (idx >= 0) {
                    value = value.substring(idx + 1)
                    break
                }
            }
            return value.trim()
        }
}

enum class NotificationKind {
    BOOK, LOTTERY, STATUS, ALERT, TEST, SYSTEM;

    companion object {
        fun classify(item: NotificationItem): NotificationKind {
            val t = item.type.lowercase().replace("_", " ")
            val blob = "${item.title} ${item.body}".lowercase()

            if (t.contains("test") || blob.contains("sse test") || blob.contains("test push")) return TEST
            if (t.contains("error") || t.contains("block") || t.contains("alert")
                || t.contains("403") || t.contains("fail")) return ALERT
            if (t.contains("status") || t.contains("change") || blob.contains("→")) return STATUS
            if (t.contains("new listing") || t.contains("listing") || t.contains("booking")) {
                if (blob.contains("lottery")) return LOTTERY
                return BOOK
            }
            return SYSTEM
        }
    }
}
