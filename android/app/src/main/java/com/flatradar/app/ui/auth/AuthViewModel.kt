package com.flatradar.app.ui.auth

import android.app.Application
import android.app.Activity
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.local.BiometricAuth
import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiException
import com.flatradar.app.data.remote.AuthInterceptor
import com.flatradar.app.domain.model.LoginRequest
import com.flatradar.app.domain.model.UserInfo
import com.flatradar.app.navigation.NavigationCoordinator
import com.flatradar.app.push.FcmTokenManager
import com.flatradar.app.ui.components.AppErrorBus
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Authentication ViewModel — matches iOS AuthStore.
 *
 * Handles login (admin/user), register, guest entry, logout, session restore.
 */
@HiltViewModel
class AuthViewModel @Inject constructor(
    private val apiClient: ApiClient,
    private val tokenManager: TokenManager,
    private val biometricAuth: BiometricAuth,
    private val authInterceptor: AuthInterceptor,
    private val navigationCoordinator: NavigationCoordinator,
    private val fcmTokenManager: FcmTokenManager,
    private val errorBus: AppErrorBus,
    application: Application
) : AndroidViewModel(application) {

    data class AuthUiState(
        val isAuthenticated: Boolean = false,
        val isLoading: Boolean = false,
        val isBiometricAuthenticating: Boolean = false,
        val canUseBiometric: Boolean = false,
        val hasBiometricCredential: Boolean = false,
        val biometryName: String = "Biometrics",
        val role: String = "guest",
        val userInfo: UserInfo? = null,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(AuthUiState())
    val uiState = _uiState.asStateFlow()

    private val deviceName: String
        get() = "${Build.MANUFACTURER} ${Build.MODEL}"

    init {
        refreshBiometricState()
        observeAuthFailures()
    }

    fun restoreSession() {
        val token = tokenManager.getToken() ?: return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true)
            try {
                val resp = apiClient.auth.getMe()
                if (resp.ok && resp.data != null) {
                    applyMe(resp.data.role, resp.data.user)
                }
            } catch (e: ApiException) {
                if (e.isAuthError) {
                    tokenManager.clearToken()
                }
                _uiState.value = _uiState.value.copy(isLoading = false)
            } catch (_: Exception) {
                _uiState.value = _uiState.value.copy(isLoading = false)
            }
        }
    }

    fun loginAsAdmin(password: String) {
        login("__admin__", password)
    }

    fun loginAsUser(username: String, password: String, saveForBiometric: Boolean = false) {
        login(username, password, saveForBiometric)
    }

    private fun login(username: String, password: String, saveForBiometric: Boolean = false) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val body = LoginRequest(username, password, deviceName)
                val resp = apiClient.auth.login(body)
                if (resp.ok && resp.data != null) {
                    tokenManager.saveToken(resp.data.token)
                    val meResp = apiClient.auth.getMe()
                    if (meResp.ok && meResp.data != null) {
                        if (saveForBiometric && meResp.data.role == "user") {
                            biometricAuth.saveCredentials(username, password, meResp.data.role)
                        }
                        applyMe(meResp.data.role, meResp.data.user)
                    }
                } else {
                    val message = resp.error?.message ?: "Login failed"
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = message
                    )
                    errorBus.show(message)
                }
            } catch (e: ApiException) {
                errorBus.show(e.message)
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = e.message
                )
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

    fun register(username: String, password: String) {
        register(username, password, saveForBiometric = false)
    }

    fun register(username: String, password: String, saveForBiometric: Boolean = false) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val body = LoginRequest(username, password, deviceName)
                val resp = apiClient.auth.register(body)
                if (resp.ok && resp.data != null) {
                    tokenManager.saveToken(resp.data.token)
                    val meResp = apiClient.auth.getMe()
                    if (meResp.ok && meResp.data != null) {
                        if (saveForBiometric && meResp.data.role == "user") {
                            biometricAuth.saveCredentials(username, password, meResp.data.role)
                        }
                        applyMe(meResp.data.role, meResp.data.user)
                    }
                } else {
                    val message = resp.error?.message ?: "Registration failed"
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = message
                    )
                    errorBus.show(message)
                }
            } catch (e: ApiException) {
                errorBus.show(e.message)
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = e.message
                )
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

    fun loginWithBiometrics(activity: Activity) {
        if (_uiState.value.isBiometricAuthenticating || _uiState.value.isLoading) return
        _uiState.value = _uiState.value.copy(isBiometricAuthenticating = true, errorMessage = null)
        biometricAuth.authenticateAndLoad(
            activity = activity,
            onSuccess = { credential ->
                _uiState.value = _uiState.value.copy(isBiometricAuthenticating = false)
                login(credential.username, credential.password)
            },
            onError = { message ->
                errorBus.show(message)
                _uiState.value = _uiState.value.copy(
                    isBiometricAuthenticating = false,
                    errorMessage = message
                )
                refreshBiometricState()
            }
        )
    }

    fun enterAsGuest() {
        _uiState.value = AuthUiState(
            isAuthenticated = true,
            role = "guest",
            userInfo = null
        )
    }

    fun logout() {
        viewModelScope.launch {
            try { apiClient.auth.logout() } catch (_: Exception) {}
            clearAuth()
        }
    }

    fun deleteAccount() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val resp = apiClient.me.deleteAccount()
                if (resp.ok) {
                    clearAuth(deleteBiometric = true)
                } else {
                    val message = resp.error?.message ?: "Unable to delete account"
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

    private fun clearAuth(deleteBiometric: Boolean = false) {
        fcmTokenManager.unregisterCurrentDevice()
        tokenManager.clearToken()
        if (deleteBiometric) {
            biometricAuth.deleteCredentials()
        }
        navigationCoordinator.reset()
        _uiState.value = AuthUiState(
            canUseBiometric = biometricAuth.isAvailable(),
            hasBiometricCredential = biometricAuth.hasStoredUserCredentials(),
            biometryName = biometricAuth.biometryName()
        )
    }

    private fun applyMe(role: String, user: UserInfo?) {
        fcmTokenManager.registerCurrentDevice()
        _uiState.value = AuthUiState(
            isAuthenticated = true,
            isLoading = false,
            canUseBiometric = biometricAuth.isAvailable(),
            hasBiometricCredential = biometricAuth.hasStoredUserCredentials(),
            biometryName = biometricAuth.biometryName(),
            role = role,
            userInfo = user
        )
    }

    private fun refreshBiometricState() {
        _uiState.value = _uiState.value.copy(
            canUseBiometric = biometricAuth.isAvailable(),
            hasBiometricCredential = biometricAuth.hasStoredUserCredentials(),
            biometryName = biometricAuth.biometryName()
        )
    }

    private fun observeAuthFailures() {
        viewModelScope.launch {
            authInterceptor.authFailures.collect {
                if (_uiState.value.isAuthenticated && _uiState.value.role != "guest") {
                    errorBus.show("Session expired. Please sign in again.")
                    clearAuth()
                }
            }
        }
    }

    val isAdmin: Boolean get() = _uiState.value.role == "admin"
    val isUser: Boolean get() = _uiState.value.role == "user"
    val isGuest: Boolean get() = _uiState.value.role == "guest"
}
