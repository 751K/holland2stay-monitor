package com.flatradar.app.ui.admin

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AdminMonitorScreen(
    onBack: () -> Unit,
    viewModel: AdminViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(Unit) { viewModel.loadMonitorStatus() }
    LaunchedEffect(state.errorMessage) {
        state.errorMessage?.let { snackbarHostState.showSnackbar(it) }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Monitor Control") },
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
            if (state.isLoadingMonitor && state.monitorStatus == null) {
                CircularProgressIndicator(Modifier.align(Alignment.CenterHorizontally))
                return@Column
            }
            val monitor = state.monitorStatus
            Text(
                if (monitor?.running == true) "Running" else "Stopped",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                color = if (monitor?.running == true) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error
            )
            Text("PID: ${monitor?.pid ?: "—"}")
            Text("Last scrape: ${monitor?.lastScrape?.takeIf { it.isNotBlank() } ?: "—"}")
            Text("Last count: ${monitor?.lastCount?.takeIf { it.isNotBlank() } ?: "—"}")

            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = { viewModel.monitorAction(MonitorAction.START) },
                    enabled = !state.actionInFlight,
                    modifier = Modifier.weight(1f)
                ) { Text("Start") }
                OutlinedButton(
                    onClick = { viewModel.monitorAction(MonitorAction.STOP) },
                    enabled = !state.actionInFlight,
                    modifier = Modifier.weight(1f)
                ) { Text("Stop") }
            }
            OutlinedButton(
                onClick = { viewModel.monitorAction(MonitorAction.RELOAD) },
                enabled = !state.actionInFlight,
                modifier = Modifier.fillMaxWidth()
            ) { Text("Reload Config") }
        }
    }
}
