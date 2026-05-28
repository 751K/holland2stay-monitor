package com.flatradar.app.ui.listings

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.FilterList
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * Listings list screen — matches Material 3 Design Specifications.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ListingsScreen(
    onOpenDetail: (String) -> Unit = {},
    onBack: () -> Unit = {},
    viewModel: ListingsViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    var showFilterSheet by remember { mutableStateOf(false) }
    var searchText by remember { mutableStateOf(state.searchQuery) }
    var active by remember { mutableStateOf(false) }

    Scaffold { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
        ) {
            // 1. SearchBar (Docked at top)
            SearchBar(
                inputField = {
                    SearchBarDefaults.InputField(
                        query = searchText,
                        onQueryChange = { searchText = it },
                        onSearch = { 
                            viewModel.updateSearch(it)
                            active = false
                        },
                        expanded = active,
                        onExpandedChange = { active = it },
                        placeholder = { Text("Search listings…") },
                        leadingIcon = { 
                            if (active) {
                                IconButton(onClick = { active = false }) {
                                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                                }
                            } else {
                                Icon(Icons.Filled.Search, contentDescription = "Search")
                            }
                        },
                        trailingIcon = {
                            if (searchText.isNotEmpty()) {
                                IconButton(onClick = { searchText = "" }) {
                                    Icon(Icons.Filled.Close, contentDescription = "Clear")
                                }
                            } else {
                                IconButton(onClick = { showFilterSheet = true }) {
                                    Icon(Icons.Filled.FilterList, contentDescription = "Filters")
                                }
                            }
                        }
                    )
                },
                expanded = active,
                onExpandedChange = { active = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = if (active) 0.dp else 16.dp, vertical = if (active) 0.dp else 8.dp)
            ) {
                // Search history or suggestions could go here
            }



            // 2. Filter Chips Row
            FilterChipsRow(
                activeFilters = state.filters,
                onFilterClick = { showFilterSheet = true }
            )

            // 3. Pull-to-refresh + Content
            val pullRefreshState = rememberPullToRefreshState()
            PullToRefreshBox(
                isRefreshing = state.isLoading && state.items.isNotEmpty(),
                onRefresh = { viewModel.load() },
                state = pullRefreshState
            ) {
                val listState = rememberLazyListState()

                // Infinite scroll logic
                val shouldLoadMore = remember {
                    derivedStateOf {
                        val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()
                            ?: return@derivedStateOf false
                        lastVisible.index >= state.items.size - 5
                    }
                }
                LaunchedEffect(shouldLoadMore.value) {
                    if (shouldLoadMore.value) viewModel.loadMore()
                }

                when {
                    state.isLoading && state.items.isEmpty() -> {
                        Box(
                            modifier = Modifier.fillMaxSize(),
                            contentAlignment = Alignment.Center
                        ) { CircularProgressIndicator() }
                    }
                    state.errorMessage != null && state.items.isEmpty() -> {
                        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Text(state.errorMessage ?: "Unable to Load",
                                    color = MaterialTheme.colorScheme.error)
                                Spacer(modifier = Modifier.height(12.dp))
                                Button(onClick = { viewModel.load() }) { Text("Try Again") }
                            }
                        }
                    }
                    state.items.isEmpty() && !state.isLoading -> {
                        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Text("No listings found", style = MaterialTheme.typography.bodyLarge)
                                if (state.filters.isActive) {
                                    TextButton(onClick = { viewModel.clearFilters() }) {
                                        Text("Clear filters")
                                    }
                                }
                            }
                        }
                    }
                    else -> {
                        LazyColumn(
                            state = listState,
                            modifier = Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(bottom = 16.dp)
                        ) {
                            item {
                                Text(
                                    "${state.total} results · sorted by newest",
                                    style = MaterialTheme.typography.labelLarge,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                                )
                            }
                            items(state.items, key = { it.id }) { listing ->
                                ListingRow(
                                    listing = listing,
                                    onClick = { onOpenDetail(listing.id) }
                                )
                            }

                            if (state.isLoadingMore) {
                                item {
                                    Box(
                                        modifier = Modifier.fillMaxWidth().padding(16.dp),
                                        contentAlignment = Alignment.Center
                                    ) { CircularProgressIndicator(modifier = Modifier.size(24.dp)) }
                                }
                            }

                            item {
                                Text(
                                    "${state.items.size} of ${state.total} listings",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    textAlign = TextAlign.Center,
                                    modifier = Modifier.fillMaxWidth().padding(16.dp)
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    if (showFilterSheet) {
        FilterSheet(
            current = state.filters,
            options = state.filterOptions,
            onApply = { filters ->
                viewModel.updateFilters(filters)
                showFilterSheet = false
            },
            onDismiss = { showFilterSheet = false }
        )
    }
}

@Composable
private fun FilterChipsRow(
    activeFilters: ListingsViewModel.ListingFilters,
    onFilterClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        val cityLabel = when {
            activeFilters.cities.isEmpty() -> "City"
            activeFilters.cities.size == 1 -> activeFilters.cities.first()
            else -> "Cities (${activeFilters.cities.size})"
        }
        FilterChip(
            selected = activeFilters.cities.isNotEmpty(),
            onClick = onFilterClick,
            label = { Text(cityLabel) },
            leadingIcon = if (activeFilters.cities.isNotEmpty()) {
                { Icon(Icons.Filled.Close, null, modifier = Modifier.size(18.dp)) }
            } else null
        )
        FilterChip(
            selected = activeFilters.contract != null,
            onClick = onFilterClick,
            label = { Text(activeFilters.contract ?: "Contract") }
        )
        FilterChip(
            selected = activeFilters.energy != null,
            onClick = onFilterClick,
            label = { Text("Energy: ${activeFilters.energy ?: "Any"}") }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FilterSheet(
    current: ListingsViewModel.ListingFilters,
    options: com.flatradar.app.data.remote.FilterOptionsResponse,
    onApply: (ListingsViewModel.ListingFilters) -> Unit,
    onDismiss: () -> Unit
) {
    var selectedCities by remember { mutableStateOf(current.cities.toSet()) }
    var status by remember { mutableStateOf(current.status ?: "") }
    var contract by remember { mutableStateOf(current.contract ?: "") }
    var energy by remember { mutableStateOf(current.energy ?: "") }

    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState
    ) {
        Column(
            modifier = Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp)
                .padding(bottom = 32.dp)
        ) {
            Text("Filters", style = MaterialTheme.typography.headlineMedium)
            Spacer(modifier = Modifier.height(16.dp))

            FilterMultiChipGroup(
                label = "City",
                selectedValues = selectedCities,
                options = options.cities,
                onToggle = { option ->
                    selectedCities = if (option in selectedCities) {
                        selectedCities - option
                    } else {
                        selectedCities + option
                    }
                },
                onClear = { selectedCities = emptySet() }
            )
            Spacer(modifier = Modifier.height(12.dp))

            FilterChipGroup(
                label = "Status",
                selectedValue = status,
                options = listOf("Available to book", "Available in lottery", "Rented"),
                onSelected = { status = it }
            )
            Spacer(modifier = Modifier.height(12.dp))

            FilterChipGroup(
                label = "Contract type",
                selectedValue = contract,
                options = options.contract,
                onSelected = { contract = it }
            )
            Spacer(modifier = Modifier.height(12.dp))

            FilterChipGroup(
                label = "Energy (min)",
                selectedValue = energy,
                options = options.energy,
                onSelected = { energy = it }
            )
            Spacer(modifier = Modifier.height(24.dp))

            Button(
                onClick = {
                    onApply(ListingsViewModel.ListingFilters(
                        cities = selectedCities.toList(),
                        status = status.takeIf { it.isNotBlank() },
                        contract = contract.takeIf { it.isNotBlank() },
                        energy = energy.takeIf { it.isNotBlank() }
                    ))
                },
                modifier = Modifier.fillMaxWidth().height(50.dp)
            ) { Text("Apply") }
        }
    }
}

@Composable
private fun FilterChipGroup(
    label: String,
    selectedValue: String,
    options: List<String>,
    onSelected: (String) -> Unit
) {
    Column {
        Text(
            text = label,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(6.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            FilterChip(
                selected = selectedValue.isEmpty(),
                onClick = { onSelected("") },
                label = { Text("Any") }
            )
            options.forEach { option ->
                FilterChip(
                    selected = selectedValue == option,
                    onClick = { onSelected(option) },
                    label = { Text(option) }
                )
            }
        }
    }
}

@Composable
private fun FilterMultiChipGroup(
    label: String,
    selectedValues: Set<String>,
    options: List<String>,
    onToggle: (String) -> Unit,
    onClear: () -> Unit
) {
    Column {
        Text(
            text = label,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(6.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            FilterChip(
                selected = selectedValues.isEmpty(),
                onClick = onClear,
                label = { Text("Any") }
            )
            options.forEach { option ->
                FilterChip(
                    selected = option in selectedValues,
                    onClick = { onToggle(option) },
                    label = { Text(option) }
                )
            }
        }
    }
}
