package com.flatradar.app.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.FilterOptionsResponse
import com.flatradar.app.domain.model.ListingFilter
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class FilterEditViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {

    data class FilterEditUiState(
        val options: FilterOptionsResponse = FilterOptionsResponse(),
        val currentFilter: ListingFilter = ListingFilter(),
        val selectedCities: Set<String> = emptySet(),
        val selectedSources: Set<String> = emptySet(),
        val selectedTypes: Set<String> = emptySet(),
        val selectedOccupancy: Set<String> = emptySet(),
        val selectedContract: Set<String> = emptySet(),
        val selectedTenant: Set<String> = emptySet(),
        val selectedFinishing: Set<String> = emptySet(),
        val selectedEnergy: Set<String> = emptySet(),
        val minRent: String = "",
        val maxRent: String = "",
        val minArea: String = "",
        val maxArea: String = "",
        val minFloor: String = "",
        val maxFloor: String = "",
        val isLoading: Boolean = false,
        val isSaving: Boolean = false,
        val message: String? = null
    )

    private val _uiState = MutableStateFlow(FilterEditUiState())
    val uiState = _uiState.asStateFlow()

    init { load() }

    fun load() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val optsResp = apiClient.me.getFilterOptions()
                val filterResp = apiClient.me.getMeFilter()
                val options = if (optsResp.ok && optsResp.data != null) optsResp.data else FilterOptionsResponse()
                val filter = if (filterResp.ok && filterResp.data != null) filterResp.data.filter else ListingFilter()

                _uiState.value = _uiState.value.copy(
                    options = options,
                    currentFilter = filter,
                    selectedCities = filter.cities.toSet(),
                    selectedSources = filter.sources.toSet(),
                    selectedTypes = filter.types.toSet(),
                    selectedOccupancy = filter.occupancy.toSet(),
                    selectedContract = filter.contract.toSet(),
                    selectedTenant = filter.tenant.toSet(),
                    selectedFinishing = filter.finishing.toSet(),
                    selectedEnergy = filter.energy.toSet(),
                    minRent = filter.minRent?.toString() ?: "",
                    maxRent = filter.maxRent?.toString() ?: "",
                    minArea = filter.minArea?.toString() ?: "",
                    maxArea = filter.maxArea?.toString() ?: "",
                    minFloor = filter.minFloor?.toString() ?: "",
                    maxFloor = filter.maxFloor?.toString() ?: "",
                    isLoading = false
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    message = e.localizedMessage ?: "Failed to load"
                )
            }
        }
    }

    fun save() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isSaving = true, message = null)
            try {
                val s = _uiState.value
                val filter = ListingFilter(
                    cities = s.selectedCities.toList(),
                    sources = s.selectedSources.toList(),
                    types = s.selectedTypes.toList(),
                    occupancy = s.selectedOccupancy.toList(),
                    contract = s.selectedContract.toList(),
                    tenant = s.selectedTenant.toList(),
                    finishing = s.selectedFinishing.toList(),
                    energy = s.selectedEnergy.toList(),
                    minRent = s.minRent.toIntOrNull(),
                    maxRent = s.maxRent.toIntOrNull(),
                    minArea = s.minArea.toIntOrNull(),
                    maxArea = s.maxArea.toIntOrNull(),
                    minFloor = s.minFloor.toIntOrNull(),
                    maxFloor = s.maxFloor.toIntOrNull()
                )
                val resp = apiClient.me.updateMeFilter(filter)
                if (resp.ok) {
                    _uiState.value = _uiState.value.copy(
                        isSaving = false,
                        currentFilter = filter,
                        message = "Filter saved"
                    )
                } else {
                    _uiState.value = _uiState.value.copy(
                        isSaving = false,
                        message = resp.error?.message ?: "Failed to save"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isSaving = false,
                    message = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun toggleChip(set: Set<String>, value: String, update: (Set<String>) -> Unit) {
        val newSet = if (value in set) set - value else set + value
        update(newSet)
    }

    fun updateSelected(update: (FilterEditUiState) -> FilterEditUiState) {
        _uiState.value = update(_uiState.value)
    }
}
