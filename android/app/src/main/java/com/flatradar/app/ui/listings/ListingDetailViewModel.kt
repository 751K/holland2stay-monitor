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

@HiltViewModel
class ListingDetailViewModel @Inject constructor(
    private val apiClient: ApiClient,
    private val errorBus: AppErrorBus
) : ViewModel() {

    data class DetailUiState(
        val listing: Listing? = null,
        val isLoading: Boolean = false,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(DetailUiState())
    val uiState = _uiState.asStateFlow()

    fun load(id: String) {
        viewModelScope.launch {
            _uiState.value = DetailUiState(isLoading = true)
            try {
                val resp = apiClient.listings.getListing(id)
                if (resp.ok && resp.data != null) {
                    _uiState.value = DetailUiState(listing = resp.data)
                } else {
                    val message = resp.error?.message ?: "Listing not found"
                    _uiState.value = DetailUiState(errorMessage = message)
                    errorBus.show(message)
                }
            } catch (e: Exception) {
                val message = e.localizedMessage ?: "Network error"
                _uiState.value = DetailUiState(errorMessage = message)
                errorBus.show(message)
            }
        }
    }

    fun reportError(message: String) {
        errorBus.show(message)
    }
}
