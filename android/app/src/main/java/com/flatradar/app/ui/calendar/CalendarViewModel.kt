package com.flatradar.app.ui.calendar

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.util.ServerTime
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.time.LocalDate
import java.time.YearMonth
import java.time.format.DateTimeParseException
import javax.inject.Inject

@HiltViewModel
class CalendarViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {

    data class CalendarUiState(
        val listings: List<Listing> = emptyList(),
        val byDay: Map<String, List<Listing>> = emptyMap(),
        val visibleMonth: YearMonth = YearMonth.now(ServerTime.zone),
        val selectedDate: LocalDate? = null,
        val isLoading: Boolean = false,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(CalendarUiState())
    val uiState = _uiState.asStateFlow()

    init { load() }

    fun load() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val resp = apiClient.listings.getCalendar()
                if (resp.ok && resp.data != null) {
                    val keyedItems = resp.data.allListings().mapNotNull { listing ->
                        val key = listing.calendarDayKey() ?: return@mapNotNull null
                        key to listing
                    }
                    val items = keyedItems.map { it.second }
                    val grouped = keyedItems.groupBy(
                        keySelector = { it.first },
                        valueTransform = { it.second }
                    )
                    val currentMonth = YearMonth.now(ServerTime.zone)
                    _uiState.value = CalendarUiState(
                        listings = items,
                        byDay = grouped,
                        visibleMonth = currentMonth,
                        selectedDate = null,
                        isLoading = false
                    )
                } else {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = resp.error?.message ?: "Failed to load calendar"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = CalendarUiState(
                    isLoading = false,
                    errorMessage = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun selectDate(date: LocalDate) {
        _uiState.value = _uiState.value.copy(
            selectedDate = date,
            visibleMonth = YearMonth.from(date)
        )
    }

    fun previousMonth() {
        _uiState.value = _uiState.value.copy(
            visibleMonth = _uiState.value.visibleMonth.minusMonths(1),
            selectedDate = null
        )
    }

    fun nextMonth() {
        _uiState.value = _uiState.value.copy(
            visibleMonth = _uiState.value.visibleMonth.plusMonths(1),
            selectedDate = null
        )
    }

    private fun Listing.calendarDayKey(): String? {
        val raw = availableFrom?.takeIf { it.isNotBlank() }
            ?: availableFromRaw?.takeIf { it.isNotBlank() }
            ?: return null
        val key = raw.trim().take(10)
        if (key.length < 10) return null
        return try {
            LocalDate.parse(key).toString()
        } catch (_: DateTimeParseException) {
            null
        }
    }
}
