package com.flatradar.app.ui.settings

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun FilterEditScreen(
    onBack: () -> Unit = {},
    viewModel: FilterEditViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Edit Filters") },
                navigationIcon = {
                    TextButton(onClick = onBack) { Text("Cancel") }
                },
                actions = {
                    TextButton(
                        onClick = { viewModel.save() },
                        enabled = !state.isSaving
                    ) {
                        if (state.isSaving) CircularProgressIndicator(Modifier.size(18.dp))
                        else Text("Save")
                    }
                }
            )
        },
        snackbarHost = {
            state.message?.let { msg ->
                Snackbar(modifier = Modifier.padding(16.dp)) { Text(msg) }
            }
        }
    ) { padding ->
        if (state.isLoading) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = androidx.compose.ui.Alignment.Center) {
                CircularProgressIndicator()
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                item { FilterSection("Cities") { ChipGrid(state.options.cities, state.selectedCities) { v -> viewModel.updateSelected { s -> val n = s.selectedCities.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedCities = n) } } } }
                item { FilterSection("Platforms") { ChipGrid(state.options.sources, state.selectedSources) { v -> viewModel.updateSelected { s -> val n = s.selectedSources.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedSources = n) } } } }
                item { FilterSection("Property Type") { ChipGrid(state.options.types, state.selectedTypes) { v -> viewModel.updateSelected { s -> val n = s.selectedTypes.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedTypes = n) } } } }
                item { FilterSection("Occupancy") { ChipGrid(state.options.occupancy, state.selectedOccupancy) { v -> viewModel.updateSelected { s -> val n = s.selectedOccupancy.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedOccupancy = n) } } } }
                item { FilterSection("Contract") { ChipGrid(state.options.contract, state.selectedContract) { v -> viewModel.updateSelected { s -> val n = s.selectedContract.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedContract = n) } } } }
                item { FilterSection("Tenant") { ChipGrid(state.options.tenant, state.selectedTenant) { v -> viewModel.updateSelected { s -> val n = s.selectedTenant.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedTenant = n) } } } }
                item { FilterSection("Finishing") { ChipGrid(state.options.finishing, state.selectedFinishing) { v -> viewModel.updateSelected { s -> val n = s.selectedFinishing.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedFinishing = n) } } } }
                item { FilterSection("Energy Label") { ChipGrid(state.options.energy, state.selectedEnergy) { v -> viewModel.updateSelected { s -> val n = s.selectedEnergy.toMutableSet(); if (v in n) n -= v else n += v; s.copy(selectedEnergy = n) } } } }

                // Numeric fields
                item {
                    FilterSection("Price Range") {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedTextField(
                                value = state.minRent,
                                onValueChange = { viewModel.updateSelected { s -> s.copy(minRent = it) } },
                                label = { Text("Min €") },
                                singleLine = true,
                                modifier = Modifier.weight(1f)
                            )
                            OutlinedTextField(
                                value = state.maxRent,
                                onValueChange = { viewModel.updateSelected { s -> s.copy(maxRent = it) } },
                                label = { Text("Max €") },
                                singleLine = true,
                                modifier = Modifier.weight(1f)
                            )
                        }
                    }
                }
                item {
                    FilterSection("Area Range (m²)") {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedTextField(
                                value = state.minArea,
                                onValueChange = { viewModel.updateSelected { s -> s.copy(minArea = it) } },
                                label = { Text("Min") },
                                singleLine = true,
                                modifier = Modifier.weight(1f)
                            )
                            OutlinedTextField(
                                value = state.maxArea,
                                onValueChange = { viewModel.updateSelected { s -> s.copy(maxArea = it) } },
                                label = { Text("Max") },
                                singleLine = true,
                                modifier = Modifier.weight(1f)
                            )
                        }
                    }
                }
                item { Spacer(Modifier.height(32.dp)) }
            }
        }
    }
}

@Composable
private fun FilterSection(title: String, content: @Composable () -> Unit) {
    Column {
        Text(title, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(8.dp))
        content()
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ChipGrid(options: List<String>, selected: Set<String>, onToggle: (String) -> Unit) {
    if (options.isEmpty()) {
        Text("No options available", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        options.forEach { option ->
            FilterChip(
                selected = option in selected,
                onClick = { onToggle(option) },
                label = { Text(option, style = MaterialTheme.typography.labelSmall) }
            )
        }
    }
}
