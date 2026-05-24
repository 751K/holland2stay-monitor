package com.flatradar.app.ui.listings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.ui.components.AppErrorBus
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

import com.flatradar.app.data.remote.FilterOptionsResponse

@HiltViewModel
class ListingsViewModel @Inject constructor(
    private val apiClient: ApiClient,
    private val errorBus: AppErrorBus
) : ViewModel() {

    data class ListingsUiState(
        val items: List<Listing> = emptyList(),
        val total: Int = 0,
        val isLoading: Boolean = false,
        val isLoadingMore: Boolean = false,
        val errorMessage: String? = null,
        val searchQuery: String = "",
        val filters: ListingFilters = ListingFilters(),
        val sortMode: SortMode = SortMode.NEWEST,
        val filterOptions: FilterOptionsResponse = FilterOptionsResponse()
    )

    data class ListingFilters(
        val city: String? = null,
        val cities: List<String> = emptyList(),
        val status: String? = null,
        val types: List<String> = emptyList(),
        val sources: List<String> = emptyList(),
        val contract: String? = null,
        val energy: String? = null
    ) {
        val isActive: Boolean
            get() = city != null || cities.isNotEmpty() || status != null ||
                types.isNotEmpty() || sources.isNotEmpty() || contract != null || energy != null
    }

    enum class SortMode { NEWEST, PRICE_ASC, PRICE_DESC }

    private val _uiState = MutableStateFlow(ListingsUiState())
    val uiState = _uiState.asStateFlow()

    private var currentOffset = 0
    private var hasMore = true

    init {
        load()
        fetchFilterOptions()
    }

    private fun fetchFilterOptions() {
        viewModelScope.launch {
            try {
                val resp = apiClient.me.getFilterOptions()
                if (resp.ok && resp.data != null) {
                    _uiState.value = _uiState.value.copy(filterOptions = resp.data)
                }
            } catch (_: Exception) {
                // Non-blocking fallback
            }
        }
    }

    fun load() {
        viewModelScope.launch {
            currentOffset = 0
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val filters = _uiState.value.filters
                val resp = apiClient.listings.getListings(
                    limit = 50, offset = 0,
                    city = filters.city,
                    cities = filters.cities.takeIf { it.isNotEmpty() }?.joinToString(","),
                    status = filters.status,
                    query = _uiState.value.searchQuery.takeIf { it.isNotBlank() },
                    sources = filters.sources.takeIf { it.isNotEmpty() }?.joinToString(","),
                    types = filters.types.takeIf { it.isNotEmpty() }?.joinToString(","),
                    contract = filters.contract,
                    energy = filters.energy
                )
                if (resp.ok && resp.data != null) {
                    _uiState.value = _uiState.value.copy(
                        items = resp.data.items,
                        total = resp.data.total,
                        isLoading = false
                    )
                    hasMore = resp.data.items.size >= 50
                    currentOffset = resp.data.items.size
                } else {
                    val message = resp.error?.message ?: "Failed to load listings"
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = message
                    )
                    errorBus.show(message)
                }
            } catch (e: Exception) {
                val message = e.localizedMessage ?: "Network error"
                errorBus.show(message)
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = message
                )
            }
        }
    }

    fun loadMore() {
        if (_uiState.value.isLoadingMore || !hasMore) return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoadingMore = true)
            try {
                val filters = _uiState.value.filters
                val resp = apiClient.listings.getListings(
                    limit = 50, offset = currentOffset,
                    city = filters.city,
                    cities = filters.cities.takeIf { it.isNotEmpty() }?.joinToString(","),
                    status = filters.status,
                    query = _uiState.value.searchQuery.takeIf { it.isNotBlank() },
                    sources = filters.sources.takeIf { it.isNotEmpty() }?.joinToString(","),
                    types = filters.types.takeIf { it.isNotEmpty() }?.joinToString(","),
                    contract = filters.contract,
                    energy = filters.energy
                )
                if (resp.ok && resp.data != null) {
                    val newItems = _uiState.value.items + resp.data.items
                    _uiState.value = _uiState.value.copy(
                        items = newItems,
                        total = resp.data.total,
                        isLoadingMore = false
                    )
                    hasMore = resp.data.items.size >= 50
                    currentOffset = newItems.size
                } else {
                    val message = resp.error?.message ?: "Unable to load more listings"
                    _uiState.value = _uiState.value.copy(isLoadingMore = false, errorMessage = message)
                    errorBus.show(message)
                }
            } catch (e: Exception) {
                errorBus.show(e.localizedMessage ?: "Network error")
                _uiState.value = _uiState.value.copy(isLoadingMore = false)
            }
        }
    }

    fun updateSearch(query: String) {
        _uiState.value = _uiState.value.copy(searchQuery = query)
        load()
    }

    fun updateFilters(filters: ListingFilters) {
        _uiState.value = _uiState.value.copy(filters = filters)
        load()
    }

    fun clearFilters() {
        _uiState.value = _uiState.value.copy(filters = ListingFilters(), searchQuery = "")
        load()
    }
}
