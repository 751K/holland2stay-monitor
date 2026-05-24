package com.flatradar.app.ui.notifications

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.SseClient
import com.flatradar.app.domain.model.NotificationItem
import com.squareup.moshi.JsonAdapter
import com.squareup.moshi.Moshi
import com.squareup.moshi.Types
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import javax.inject.Inject
import kotlin.math.min

@HiltViewModel
class NotificationsViewModel @Inject constructor(
    private val apiClient: ApiClient,
    private val sseClient: SseClient,
    private val tokenManager: TokenManager
) : ViewModel() {

    data class NotificationsUiState(
        val items: List<NotificationItem> = emptyList(),
        val unreadCount: Int = 0,
        val isLoading: Boolean = false,
        val isLive: Boolean = false
    )

    private val _uiState = MutableStateFlow(NotificationsUiState())
    val uiState = _uiState.asStateFlow()

    private val moshi = Moshi.Builder().build()
    private val notificationListAdapter: JsonAdapter<List<NotificationItem>> =
        moshi.adapter(Types.newParameterizedType(List::class.java, NotificationItem::class.java))
    private val notificationAdapter: JsonAdapter<NotificationItem> =
        moshi.adapter(NotificationItem::class.java)

    private var sseJob: Job? = null
    private var lastId: Int = 0

    init { load() }

    fun load() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val resp = apiClient.notifications.getNotifications(limit = 100, offset = 0)
                if (resp.ok && resp.data != null) {
                    val items = resp.data.items
                    if (items.isNotEmpty()) {
                        lastId = items.maxOf { it.id }
                    }
                    _uiState.value = NotificationsUiState(
                        items = items,
                        unreadCount = resp.data.unread,
                        isLive = true
                    )
                }
            } catch (_: Exception) {
                _uiState.value = NotificationsUiState(isLoading = false)
            }
            startSse()
        }
    }

    private fun startSse() {
        sseJob?.cancel()
        sseJob = viewModelScope.launch {
            var backoff = 2000L
            val maxBackoff = 60_000L

            while (isActive) {
                try {
                    sseClient.connect(
                        baseUrl = apiClient.baseUrl,
                        token = tokenManager.getToken(),
                        lastId = lastId
                    ).collect { event ->
                        backoff = 2000L // reset on success
                        when (event) {
                            is SseClient.SseEvent.Data -> {
                                val items = parseSseData(event.payload)
                                if (items.isNotEmpty()) {
                                    mergeNewItems(items)
                                    lastId = items.maxOf { it.id }
                                }
                            }
                            is SseClient.SseEvent.Retry -> {
                                backoff = event.ms.toLong()
                            }
                            is SseClient.SseEvent.Keepalive -> {
                                _uiState.value = _uiState.value.copy(isLive = true)
                            }
                        }
                    }
                    // Stream ended normally — reconnect after delay
                    delay(backoff)
                } catch (e: Exception) {
                    if (!isActive) break
                    _uiState.value = _uiState.value.copy(isLive = false)
                    delay(backoff)
                    backoff = min(backoff * 2, maxBackoff)
                }
            }
        }
    }

    private fun parseSseData(payload: String): List<NotificationItem> {
        return try {
            notificationListAdapter.fromJson(payload) ?: emptyList()
        } catch (_: Exception) {
            try {
                listOfNotNull(notificationAdapter.fromJson(payload))
            } catch (_: Exception) {
                emptyList()
            }
        }
    }

    private fun mergeNewItems(newItems: List<NotificationItem>) {
        val current = _uiState.value.items.toMutableList()
        val existingIds = current.map { it.id }.toSet()
        val added = newItems.filter { it.id !in existingIds }
        if (added.isEmpty()) {
            // Just update read status on existing
            val updated = current.map { existing ->
                newItems.find { it.id == existing.id } ?: existing
            }
            _uiState.value = _uiState.value.copy(
                items = updated,
                unreadCount = updated.count { !it.isRead }
            )
            return
        }
        // Insert new at top, keep max 200 items
        val merged = (added + current).take(200)
        _uiState.value = _uiState.value.copy(
            items = merged,
            unreadCount = merged.count { !it.isRead },
            isLive = true
        )
    }

    fun markAllRead() {
        viewModelScope.launch {
            try {
                apiClient.notifications.markRead(com.flatradar.app.data.remote.MarkReadRequest())
                _uiState.value = _uiState.value.copy(
                    unreadCount = 0,
                    items = _uiState.value.items.map { it.copy(read = 1) }
                )
            } catch (_: Exception) {}
        }
    }

    fun markRead(id: Int) {
        val current = _uiState.value.items
        if (current.firstOrNull { it.id == id }?.isRead == true) return
        viewModelScope.launch {
            try {
                apiClient.notifications.markRead(com.flatradar.app.data.remote.MarkReadRequest(ids = listOf(id)))
                val updated = _uiState.value.items.map { item ->
                    if (item.id == id) item.copy(read = 1) else item
                }
                _uiState.value = _uiState.value.copy(
                    items = updated,
                    unreadCount = updated.count { !it.isRead }
                )
            } catch (_: Exception) {}
        }
    }
}
