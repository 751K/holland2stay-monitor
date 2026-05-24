package com.flatradar.app.ui.map

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.domain.model.Listing
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class MapViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {

    data class MapUiState(
        val listings: List<Listing> = emptyList(),
        val selectedListing: Listing? = null,
        val isLoading: Boolean = false,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(MapUiState())
    val uiState = _uiState.asStateFlow()

    init { load() }

    fun load() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val resp = apiClient.listings.getMap()
                if (resp.ok && resp.data != null) {
                    val items = resp.data.allListings().filter { it.latitude != null && it.longitude != null }
                    _uiState.value = MapUiState(listings = items, isLoading = false)
                } else {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = resp.error?.message ?: "Failed to load map listings"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = MapUiState(
                    isLoading = false,
                    errorMessage = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun selectListing(listing: Listing) {
        _uiState.value = _uiState.value.copy(selectedListing = listing)
    }

    fun clearSelection() {
        _uiState.value = _uiState.value.copy(selectedListing = null)
    }
}
