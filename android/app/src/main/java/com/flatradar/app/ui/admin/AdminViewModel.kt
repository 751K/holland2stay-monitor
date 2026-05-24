package com.flatradar.app.ui.admin

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.domain.model.AdminMonitorStatus
import com.flatradar.app.domain.model.AdminUserSummary
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class AdminViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {
    data class AdminUiState(
        val users: List<AdminUserSummary> = emptyList(),
        val totalUsers: Int = 0,
        val monitorStatus: AdminMonitorStatus? = null,
        val isLoadingUsers: Boolean = false,
        val isLoadingMonitor: Boolean = false,
        val actionInFlight: Boolean = false,
        val errorMessage: String? = null
    )

    private val _uiState = MutableStateFlow(AdminUiState())
    val uiState = _uiState.asStateFlow()

    fun loadUsers() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoadingUsers = true, errorMessage = null)
            try {
                val resp = apiClient.admin.getUsers()
                _uiState.value = if (resp.ok && resp.data != null) {
                    _uiState.value.copy(
                        users = resp.data.items,
                        totalUsers = resp.data.total,
                        isLoadingUsers = false
                    )
                } else {
                    _uiState.value.copy(
                        isLoadingUsers = false,
                        errorMessage = resp.error?.message ?: "Unable to load users"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoadingUsers = false,
                    errorMessage = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun toggleUser(id: String) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(actionInFlight = true, errorMessage = null)
            try {
                val resp = apiClient.admin.toggleUser(id)
                if (resp.ok) loadUsers() else {
                    _uiState.value = _uiState.value.copy(
                        errorMessage = resp.error?.message ?: "Unable to update user"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(errorMessage = e.localizedMessage ?: "Network error")
            } finally {
                _uiState.value = _uiState.value.copy(actionInFlight = false)
            }
        }
    }

    fun deleteUser(id: String) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(actionInFlight = true, errorMessage = null)
            try {
                val resp = apiClient.admin.deleteUser(id)
                if (resp.ok) {
                    _uiState.value = _uiState.value.copy(users = _uiState.value.users.filterNot { it.id == id })
                } else {
                    _uiState.value = _uiState.value.copy(
                        errorMessage = resp.error?.message ?: "Unable to delete user"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(errorMessage = e.localizedMessage ?: "Network error")
            } finally {
                _uiState.value = _uiState.value.copy(actionInFlight = false)
            }
        }
    }

    fun loadMonitorStatus() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoadingMonitor = true, errorMessage = null)
            try {
                val resp = apiClient.admin.getMonitorStatus()
                _uiState.value = if (resp.ok && resp.data != null) {
                    _uiState.value.copy(monitorStatus = resp.data, isLoadingMonitor = false)
                } else {
                    _uiState.value.copy(
                        isLoadingMonitor = false,
                        errorMessage = resp.error?.message ?: "Unable to load monitor status"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoadingMonitor = false,
                    errorMessage = e.localizedMessage ?: "Network error"
                )
            }
        }
    }

    fun monitorAction(action: MonitorAction) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(actionInFlight = true, errorMessage = null)
            try {
                val resp = when (action) {
                    MonitorAction.START -> apiClient.admin.startMonitor()
                    MonitorAction.STOP -> apiClient.admin.stopMonitor()
                    MonitorAction.RELOAD -> apiClient.admin.reloadMonitor()
                }
                if (!resp.ok) {
                    _uiState.value = _uiState.value.copy(
                        errorMessage = resp.error?.message ?: "Monitor action failed"
                    )
                }
                delay(600)
                loadMonitorStatus()
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(errorMessage = e.localizedMessage ?: "Network error")
            } finally {
                _uiState.value = _uiState.value.copy(actionInFlight = false)
            }
        }
    }
}

enum class MonitorAction { START, STOP, RELOAD }
