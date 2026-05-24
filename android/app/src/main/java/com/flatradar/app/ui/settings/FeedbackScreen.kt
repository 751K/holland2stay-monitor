package com.flatradar.app.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.FeedbackRequest
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class FeedbackViewModel @Inject constructor(
    private val apiClient: ApiClient
) : ViewModel() {
    data class FeedbackUiState(
        val kind: String = "bug",
        val message: String = "",
        val isSubmitting: Boolean = false,
        val notice: String? = null,
        val submitted: Boolean = false
    )

    private val _uiState = MutableStateFlow(FeedbackUiState())
    val uiState = _uiState.asStateFlow()

    fun updateKind(kind: String) {
        _uiState.value = _uiState.value.copy(kind = kind)
    }

    fun updateMessage(message: String) {
        _uiState.value = _uiState.value.copy(message = message)
    }

    fun submit(userName: String) {
        val current = _uiState.value
        if (current.message.trim().length < 5 || current.isSubmitting) return
        viewModelScope.launch {
            _uiState.value = current.copy(isSubmitting = true, notice = null)
            try {
                val resp = apiClient.feedback.submitFeedback(
                    FeedbackRequest(
                        kind = current.kind,
                        message = current.message.trim(),
                        userName = userName,
                        appVersion = "1.7.1"
                    )
                )
                _uiState.value = if (resp.ok) {
                    current.copy(isSubmitting = false, notice = "Feedback sent", submitted = true)
                } else {
                    current.copy(
                        isSubmitting = false,
                        notice = resp.error?.message ?: "Unable to send feedback"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = current.copy(
                    isSubmitting = false,
                    notice = e.localizedMessage ?: "Network error"
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FeedbackScreen(
    userName: String,
    onBack: () -> Unit,
    viewModel: FeedbackViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(state.notice) {
        state.notice?.let { snackbarHostState.showSnackbar(it) }
    }
    LaunchedEffect(state.submitted) {
        if (state.submitted) onBack()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Feedback") },
                navigationIcon = { TextButton(onClick = onBack) { Text("Back") } }
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Text("What should we look at?", style = MaterialTheme.typography.titleMedium)
            androidx.compose.foundation.layout.FlowRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                listOf("bug" to "Bug", "suggestion" to "Suggestion", "other" to "Other").forEach { (value, label) ->
                    FilterChip(
                        selected = state.kind == value,
                        onClick = { viewModel.updateKind(value) },
                        label = { Text(label) }
                    )
                }
            }
            OutlinedTextField(
                value = state.message,
                onValueChange = viewModel::updateMessage,
                label = { Text("Message") },
                supportingText = { Text("${state.message.length}/2000") },
                minLines = 6,
                maxLines = 10,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(4.dp))
            Button(
                onClick = { viewModel.submit(userName) },
                enabled = state.message.trim().length >= 5 && !state.isSubmitting,
                modifier = Modifier.fillMaxWidth()
            ) {
                if (state.isSubmitting) {
                    CircularProgressIndicator(modifier = Modifier.height(18.dp))
                } else {
                    Text("Send")
                }
            }
        }
    }
}
