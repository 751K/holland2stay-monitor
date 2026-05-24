package com.flatradar.app.ui.dashboard

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiException
import com.flatradar.app.data.remote.MeSummaryResponse
import com.flatradar.app.domain.model.*
import com.flatradar.app.ui.components.AppErrorBus
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Dashboard ViewModel — matches iOS DashboardStore.
 */
@HiltViewModel
class DashboardViewModel @Inject constructor(
    private val apiClient: ApiClient,
    private val errorBus: AppErrorBus
) : ViewModel() {

    data class DashboardUiState(
        val summary: MonitorStatus? = null,
        val meSummary: MeSummaryResponse? = null,
        val chartDailyNew: ChartData? = null,
        val chartSource: ChartData? = null,
        val chartStatus: ChartData? = null,
        val chartPrice: ChartData? = null,
        val chartType: ChartData? = null,
        val chartEnergy: ChartData? = null,
        val chartTenant: ChartData? = null,
        val matchPreviews: List<Listing> = emptyList(),
        val isLoading: Boolean = false,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(DashboardUiState())
    val uiState = _uiState.asStateFlow()

    fun fetchAll(isUser: Boolean) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                fetchSummary()
                if (isUser) {
                    fetchMeSummary()
                    fetchMatchPreviews()
                }
                fetchMiniCharts()
                _uiState.value = _uiState.value.copy(isLoading = false)
            } catch (e: Exception) {
                val message = e.localizedMessage ?: "Failed to load dashboard"
                errorBus.show(message)
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = message
                )
            }
        }
    }

    private suspend fun fetchSummary() {
        for (attempt in 0..1) {
            try {
                val resp = apiClient.stats.getPublicSummary()
                if (resp.ok && resp.data != null) {
                    _uiState.value = _uiState.value.copy(summary = resp.data)
                    return
                }
                if (!resp.ok) {
                    throw ApiException(
                        resp.error?.code ?: "dashboard_error",
                        resp.error?.message ?: "Failed to load dashboard"
                    )
                }
            } catch (e: Exception) {
                if (attempt == 1) throw e
                kotlinx.coroutines.delay(600)
            }
        }
    }

    private suspend fun fetchMeSummary() {
        try {
            val resp = apiClient.me.getMeSummary()
            if (resp.ok && resp.data != null) {
                _uiState.value = _uiState.value.copy(meSummary = resp.data)
            }
        } catch (_: Exception) { /* non-critical */ }
    }

    private suspend fun fetchMatchPreviews() {
        try {
            val resp = apiClient.listings.getListings(limit = 5, offset = 0)
            if (resp.ok && resp.data != null) {
                _uiState.value = _uiState.value.copy(matchPreviews = resp.data.items)
            }
        } catch (_: Exception) { /* non-critical */ }
    }

    private suspend fun fetchMiniCharts() {
        // Batch 1: most important
        val dn = try { apiClient.stats.getPublicChart("daily_new", 7) } catch (_: Exception) { null }
        val so = try { apiClient.stats.getPublicChart("source_dist", 30) } catch (_: Exception) { null }
        val st = try { apiClient.stats.getPublicChart("status_dist", 30) } catch (_: Exception) { null }

        // Batch 2
        val pr = try { apiClient.stats.getPublicChart("price_dist", 30) } catch (_: Exception) { null }
        val tp = try { apiClient.stats.getPublicChart("type_dist", 30) } catch (_: Exception) { null }

        // Batch 3
        val en = try { apiClient.stats.getPublicChart("energy_dist", 30) } catch (_: Exception) { null }
        val tn = try { apiClient.stats.getPublicChart("tenant_dist", 30) } catch (_: Exception) { null }

        _uiState.value = _uiState.value.copy(
            chartDailyNew = dn?.data,
            chartSource = so?.data,
            chartStatus = st?.data,
            chartPrice = pr?.data,
            chartType = tp?.data,
            chartEnergy = en?.data,
            chartTenant = tn?.data
        )
    }

    fun clear() {
        _uiState.value = DashboardUiState()
    }
}
