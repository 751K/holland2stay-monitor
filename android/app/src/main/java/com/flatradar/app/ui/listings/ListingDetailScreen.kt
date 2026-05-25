package com.flatradar.app.ui.listings

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.filled.FavoriteBorder
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.domain.model.Listing

/**
 * Material 3 Listing detail screen — restored to simple text-based style.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ListingDetailScreen(
    listingId: String,
    onBack: () -> Unit = {},
    viewModel: ListingDetailViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val context = LocalContext.current

    LaunchedEffect(listingId) {
        viewModel.load(listingId)
    }

    Scaffold(
        topBar = {
            CenterAlignedTopAppBar(
                title = { Text("Listing Details") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back")
                    }
                },
                actions = {
                    IconButton(onClick = { /* Save logic */ }) {
                        Icon(Icons.Filled.FavoriteBorder, "Save")
                    }
                }
            )
        },
        bottomBar = {
            state.listing?.let { listing ->
                Surface(
                    tonalElevation = 6.dp,
                    shadowElevation = 8.dp,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Box(
                        modifier = Modifier
                            .padding(horizontal = 20.dp, vertical = 16.dp)
                            .navigationBarsPadding()
                    ) {
                        Button(
                            onClick = {
                                val intent = Intent(Intent.ACTION_VIEW, Uri.parse(listing.url))
                                context.startActivity(intent)
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(52.dp),
                            shape = androidx.compose.foundation.shape.RoundedCornerShape(26.dp)
                        ) {
                            Icon(Icons.AutoMirrored.Filled.OpenInNew, null)
                            Spacer(Modifier.width(8.dp))
                            Text("View on ${listing.source}")
                        }
                    }
                }
            }
        }
    ) { padding ->
        when {
            state.isLoading -> {
                Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
            state.errorMessage != null -> {
                Box(modifier = Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(state.errorMessage!!, color = MaterialTheme.colorScheme.error)
                        Spacer(Modifier.height(12.dp))
                        Button(onClick = { viewModel.load(listingId) }) { Text("Retry") }
                    }
                }
            }
            state.listing != null -> {
                val listing = state.listing!!
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding)
                        .verticalScroll(rememberScrollState())
                        .padding(horizontal = 20.dp, vertical = 16.dp)
                ) {
                    Text(listing.name, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                    Spacer(modifier = Modifier.height(4.dp))

                    Text(listing.displayPrice, fontSize = 28.sp, fontWeight = FontWeight.W800, color = MaterialTheme.colorScheme.primary)
                    Spacer(modifier = Modifier.height(16.dp))

                    // Details list
                    DetailRow("Status", listing.status)
                    DetailRow("City", listing.displayCity)
                    DetailRow("Source", listing.source)
                    DetailRow("Building", listing.displayBuilding)
                    DetailRow("Area", listing.displayArea)
                    DetailRow("Rooms", listing.displayRooms)
                    DetailRow("Floor", listing.displayFloor)
                    DetailRow("Energy", listing.displayEnergy)
                    DetailRow("Finishing", listing.displayFinishing)
                    DetailRow("Occupancy", listing.displayOccupancy)
                    DetailRow("Contract", listing.displayContract)
                    DetailRow("Tenant", listing.displayTenant)
                    listing.availableFrom?.let { DetailRow("Available", it) }

                    // Feature map
                    listing.featureMap?.let { features ->
                        if (features.isNotEmpty()) {
                            Spacer(modifier = Modifier.height(24.dp))
                            Text("Features", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                            Spacer(modifier = Modifier.height(12.dp))
                            features.forEach { (key, value) ->
                                Row(modifier = Modifier.padding(vertical = 3.dp)) {
                                    Text("${key.displayKey()}: ", fontWeight = FontWeight.SemiBold, fontSize = 14.sp, modifier = Modifier.width(110.dp))
                                    Text(value, fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(100.dp))
                }
            }
        }
    }
}

@Composable
private fun DetailRow(label: String, value: String) {
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Text("$label: ", fontWeight = FontWeight.SemiBold, fontSize = 14.sp, modifier = Modifier.width(110.dp))
        Text(value, fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
    }
}

private fun String.displayKey(): String =
    replace("_", " ")
        .replace("-", " ")
        .split(" ")
        .filter { it.isNotBlank() }
        .joinToString(" ") { word ->
            word.lowercase().replaceFirstChar { it.uppercase() }
        }
