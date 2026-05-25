package com.flatradar.app.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.LegalResponse
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class LegalViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {

    data class LegalUiState(
        val terms: String? = null,
        val privacy: String? = null,
        val isLoading: Boolean = false
    )

    private val _uiState = MutableStateFlow(LegalUiState())
    val uiState = _uiState.asStateFlow()

    private var cached: LegalResponse? = null

    fun load(lang: String? = null) {
        if (cached != null) {
            _uiState.value = LegalUiState(terms = cached!!.terms, privacy = cached!!.privacy)
            return
        }
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val resp = apiClient.legal.getLegal(lang)
                if (resp.ok && resp.data != null) {
                    cached = resp.data
                    _uiState.value = LegalUiState(
                        terms = resp.data.terms,
                        privacy = resp.data.privacy
                    )
                }
            } catch (_: Exception) {
                // Use local fallback — caller handles this
            } finally {
                _uiState.value = _uiState.value.copy(isLoading = false)
            }
        }
    }
}
