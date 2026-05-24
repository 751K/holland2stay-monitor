package com.flatradar.app.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.local.AppColorScheme
import com.flatradar.app.data.local.BiometricAuth
import com.flatradar.app.data.local.PreferencesManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ChangePasswordRequest
import com.flatradar.app.domain.model.ListingFilter
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import org.json.JSONObject
import javax.inject.Inject

@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val preferencesManager: PreferencesManager,
    private val biometricAuth: BiometricAuth,
    private val apiClient: ApiClient
) : ViewModel() {
    data class SettingsUiState(
        val message: String? = null,
        val canUseBiometric: Boolean = false,
        val hasBiometricCredential: Boolean = false,
        val biometryName: String = "Biometrics",
        val isChangingPassword: Boolean = false,
        val passwordChanged: Boolean = false,
        val isExporting: Boolean = false,
        val exportJson: String? = null,
        val currentFilter: ListingFilter? = null
    )

    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState = _uiState.asStateFlow()

    init {
        refreshBiometricState()
    }

    fun saveServerUrl(rawUrl: String) {
        if (!PreferencesManager.isValidServerUrl(rawUrl)) {
            _uiState.value = SettingsUiState("Enter a valid http or https URL.")
            return
        }
        viewModelScope.launch {
            preferencesManager.setServerUrl(rawUrl)
            _uiState.value = SettingsUiState("Server URL saved")
        }
    }

    fun saveColorScheme(scheme: AppColorScheme) {
        viewModelScope.launch {
            preferencesManager.setColorScheme(scheme)
            _uiState.value = SettingsUiState("Appearance updated")
        }
    }

    fun changePassword(currentPassword: String, newPassword: String) {
        if (_uiState.value.isChangingPassword) return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isChangingPassword = true, message = null, passwordChanged = false)
            try {
                val resp = apiClient.auth.changePassword(
                    ChangePasswordRequest(
                        currentPassword = currentPassword,
                        newPassword = newPassword
                    )
                )
                if (resp.ok && resp.data != null) {
                    val count = resp.data.revokedOtherSessions
                    _uiState.value = _uiState.value.copy(
                        isChangingPassword = false,
                        passwordChanged = true,
                        message = "Password changed. Revoked $count other session${if (count == 1) "" else "s"}."
                    )
                } else {
                    _uiState.value = _uiState.value.copy(
                        isChangingPassword = false,
                        message = resp.error?.message ?: "Unable to change password"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isChangingPassword = false,
                    message = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun exportMyData() {
        if (_uiState.value.isExporting) return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isExporting = true, message = null, exportJson = null)
            try {
                val raw = apiClient.me.exportMe().string()
                val root = JSONObject(raw)
                if (!root.optBoolean("ok", false)) {
                    val error = root.optJSONObject("error")
                    _uiState.value = _uiState.value.copy(
                        isExporting = false,
                        message = error?.optString("message")?.takeIf { it.isNotBlank() }
                            ?: "Unable to export data"
                    )
                    return@launch
                }
                val data = root.opt("data") ?: throw IllegalStateException("Invalid export response")
                val pretty = when (data) {
                    is JSONObject -> data.toString(2)
                    else -> JSONObject.wrap(data)?.toString() ?: data.toString()
                }
                _uiState.value = _uiState.value.copy(
                    isExporting = false,
                    exportJson = pretty,
                    message = "Export ready"
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isExporting = false,
                    message = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun deleteBiometricSignIn() {
        biometricAuth.deleteCredentials()
        _uiState.value = _uiState.value.copy(
            hasBiometricCredential = false,
            message = "${biometricAuth.biometryName()} sign-in removed"
        )
    }

    fun loadFilter() {
        viewModelScope.launch {
            try {
                val resp = apiClient.me.getMeFilter()
                if (resp.ok && resp.data != null) {
                    _uiState.value = _uiState.value.copy(currentFilter = resp.data.filter)
                }
            } catch (e: Exception) {
                // Ignore background fetch error for filter
            }
        }
    }

    fun clearMessage() {
        _uiState.value = _uiState.value.copy(message = null)
    }

    fun clearPasswordChanged() {
        _uiState.value = _uiState.value.copy(passwordChanged = false)
    }

    fun clearExport() {
        _uiState.value = _uiState.value.copy(exportJson = null)
    }

    private fun refreshBiometricState() {
        _uiState.value = _uiState.value.copy(
            canUseBiometric = biometricAuth.isAvailable(),
            hasBiometricCredential = biometricAuth.hasStoredUserCredentials(),
            biometryName = biometricAuth.biometryName()
        )
    }
}
