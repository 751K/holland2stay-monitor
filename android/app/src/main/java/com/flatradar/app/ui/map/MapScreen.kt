package com.flatradar.app.ui.map

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Map
import androidx.compose.material.icons.filled.Place
import androidx.compose.material.icons.filled.MyLocation
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.BuildConfig
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.domain.model.StatusKind
import com.flatradar.app.ui.theme.StatusBook
import com.flatradar.app.ui.theme.StatusLottery
import com.flatradar.app.ui.theme.StatusReserved
import com.google.android.gms.maps.CameraUpdateFactory
import com.google.android.gms.maps.model.CameraPosition
import com.google.android.gms.maps.model.LatLng
import com.google.android.gms.maps.model.LatLngBounds
import com.google.maps.android.clustering.ClusterItem
import com.google.maps.android.compose.GoogleMap
import com.google.maps.android.compose.MapsComposeExperimentalApi
import com.google.maps.android.compose.MapProperties
import com.google.maps.android.compose.MapUiSettings
import com.google.maps.android.compose.rememberCameraPositionState
import com.google.maps.android.compose.clustering.Clustering
import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import kotlinx.coroutines.launch

private val NetherlandsCenter = LatLng(52.1326, 5.2913)

@Composable
fun MapScreen(
    onOpenDetail: (String) -> Unit = {},
    viewModel: MapViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val hasMapsKey = BuildConfig.MAPS_API_KEY.isNotBlank()

    when {
        state.isLoading && state.listings.isEmpty() -> {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        state.errorMessage != null && state.listings.isEmpty() -> {
            ErrorMapState(message = state.errorMessage ?: "Unable to load map", onRetry = viewModel::load)
        }
        state.listings.isEmpty() -> {
            EmptyMapState("No geocoded listings available")
        }
        !hasMapsKey -> {
            MapFallbackList(
                listings = state.listings,
                message = "Google Maps is not configured. Add MAPS_API_KEY to android/local.properties.",
                onOpenDetail = onOpenDetail
            )
        }
        else -> {
            GoogleListingsMap(
                listings = state.listings,
                isLoading = state.isLoading,
                onRefresh = viewModel::load,
                onOpenDetail = onOpenDetail
            )
        }
    }
}

@OptIn(MapsComposeExperimentalApi::class)
@Composable
private fun GoogleListingsMap(
    listings: List<Listing>,
    isLoading: Boolean,
    onRefresh: () -> Unit,
    onOpenDetail: (String) -> Unit
) {
    val markerItems = remember(listings) { listings.toMapItems() }
    var selectedCluster by remember(markerItems) { mutableStateOf<MapCluster?>(null) }
    var mapLoaded by remember { mutableStateOf(false) }
    val cameraPositionState = rememberCameraPositionState {
        position = CameraPosition.fromLatLngZoom(NetherlandsCenter, 7f)
    }

    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()

    // Location Permission Launcher
    val locationPermissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val fineGranted = permissions[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val coarseGranted = permissions[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        if (fineGranted || coarseGranted) {
            // Permission granted, trigger map location centering
            val locationManager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val providers = locationManager.getProviders(true)
            var bestLocation: Location? = null
            for (provider in providers) {
                try {
                    val l = locationManager.getLastKnownLocation(provider)
                    if (l != null) {
                        if (bestLocation == null || l.accuracy < bestLocation.accuracy) {
                            bestLocation = l
                        }
                    }
                } catch (e: SecurityException) {
                    // Ignore
                }
            }
            if (bestLocation != null) {
                coroutineScope.launch {
                    cameraPositionState.animate(
                        CameraUpdateFactory.newLatLngZoom(
                            LatLng(bestLocation.latitude, bestLocation.longitude),
                            14f
                        )
                    )
                }
            }
        }
    }

    // Function to fetch device location and center map
    fun moveToCurrentLocation() {
        val fineGranted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val coarseGranted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        if (fineGranted || coarseGranted) {
            val locationManager = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val providers = locationManager.getProviders(true)
            var bestLocation: Location? = null
            for (provider in providers) {
                try {
                    val l = locationManager.getLastKnownLocation(provider)
                    if (l != null) {
                        if (bestLocation == null || l.accuracy < bestLocation.accuracy) {
                            bestLocation = l
                        }
                    }
                } catch (e: SecurityException) {
                    // Ignore
                }
            }

            if (bestLocation != null) {
                coroutineScope.launch {
                    cameraPositionState.animate(
                        CameraUpdateFactory.newLatLngZoom(
                            LatLng(bestLocation.latitude, bestLocation.longitude),
                            14f
                        )
                    )
                }
            }

            // Request a single high-accuracy location update with 15s timeout
            val providersForUpdates = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            val handler = android.os.Handler(context.mainLooper)
            for (provider in providersForUpdates) {
                if (locationManager.isProviderEnabled(provider)) {
                    try {
                        var timeoutRunnable: Runnable? = null
                        val listener = object : LocationListener {
                            override fun onLocationChanged(location: Location) {
                                timeoutRunnable?.let { handler.removeCallbacks(it) }
                                coroutineScope.launch {
                                    cameraPositionState.animate(
                                        CameraUpdateFactory.newLatLngZoom(
                                            LatLng(location.latitude, location.longitude),
                                            14f
                                        )
                                    )
                                }
                                locationManager.removeUpdates(this)
                            }
                            override fun onProviderEnabled(provider: String) {}
                            override fun onProviderDisabled(provider: String) {}
                        }
                        timeoutRunnable = Runnable {
                            locationManager.removeUpdates(listener)
                        }
                        timeoutRunnable?.let { handler.postDelayed(it, 15_000L) }
                        locationManager.requestLocationUpdates(
                            provider, 0L, 0f, listener, context.mainLooper,
                        )
                    } catch (e: SecurityException) {
                        // Ignore
                    } catch (e: IllegalArgumentException) {
                        // Ignore
                    }
                }
            }
        } else {
            locationPermissionLauncher.launch(
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
                )
            )
        }
    }

    LaunchedEffect(mapLoaded, markerItems) {
        if (!mapLoaded) return@LaunchedEffect
        val distinctPositions = markerItems.map { it.position }.distinct()
        when (distinctPositions.size) {
            0 -> Unit
            1 -> cameraPositionState.move(
                CameraUpdateFactory.newLatLngZoom(distinctPositions.first(), 13f)
            )
            else -> cameraPositionState.move(
                CameraUpdateFactory.newLatLngBounds(distinctPositions.toLatLngBounds(), 96)
            )
        }
    }

    Box(Modifier.fillMaxSize()) {
        val hasLocationPermission = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        GoogleMap(
            modifier = Modifier.fillMaxSize(),
            cameraPositionState = cameraPositionState,
            properties = MapProperties(isMyLocationEnabled = hasLocationPermission),
            uiSettings = MapUiSettings(zoomControlsEnabled = false, mapToolbarEnabled = false),
            onMapClick = { selectedCluster = null },
            onMapLoaded = { mapLoaded = true }
        ) {
            Clustering(
                items = markerItems,
                onClusterClick = { cluster ->
                    selectedCluster = MapCluster(
                        position = cluster.position,
                        listings = cluster.items.map { it.listing }
                            .sortedBy { it.priceValue ?: Float.MAX_VALUE }
                    )
                    true
                },
                onClusterItemClick = { item ->
                    selectedCluster = MapCluster(
                        position = item.position,
                        listings = listOf(item.listing)
                    )
                    true
                },
                onClusterItemInfoWindowClick = {},
                onClusterItemInfoWindowLongClick = {},
                clusterContent = { cluster ->
                    ClusterMarker(count = cluster.size)
                },
                clusterItemContent = { item ->
                    ListingMarker(kind = item.listing.statusKind)
                }
            )
        }

        if (markerItems.isEmpty()) {
            Surface(
                modifier = Modifier
                    .align(Alignment.Center)
                    .padding(24.dp),
                tonalElevation = 2.dp,
                shape = MaterialTheme.shapes.medium
            ) {
                Text(
                    "No geocoded listings available",
                    modifier = Modifier.padding(16.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center
                )
            }
        }

        Surface(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .padding(12.dp),
            tonalElevation = 3.dp,
            shape = MaterialTheme.shapes.extraLarge,
            color = MaterialTheme.colorScheme.surfaceContainer,
            contentColor = MaterialTheme.colorScheme.onSurface
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(Icons.Filled.Map, null, tint = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.padding(horizontal = 4.dp))
                Text("${listings.size} listings on map", style = MaterialTheme.typography.labelMedium)
            }
        }

        // Floating action buttons on the right side
        Column(
            modifier = Modifier
                .align(Alignment.CenterEnd)
                .padding(end = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            // My Location navigation button
            SmallFloatingActionButton(
                onClick = { moveToCurrentLocation() },
                containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
                shape = CircleShape
            ) {
                Icon(
                    imageVector = Icons.Filled.MyLocation,
                    contentDescription = "My Location"
                )
            }

            // Refresh button
            SmallFloatingActionButton(
                onClick = { if (!isLoading) onRefresh() },
                containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
                shape = CircleShape
            ) {
                if (isLoading) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        strokeWidth = 2.dp,
                        color = MaterialTheme.colorScheme.primary
                    )
                } else {
                    Icon(
                        imageVector = Icons.Filled.Refresh,
                        contentDescription = "Refresh"
                    )
                }
            }
        }

        AnimatedVisibility(
            visible = selectedCluster != null,
            enter = slideInVertically(initialOffsetY = { it }) + fadeIn(),
            exit = slideOutVertically(targetOffsetY = { it }) + fadeOut(),
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(12.dp)
        ) {
            selectedCluster?.let { cluster ->
                SelectedClusterCard(
                    cluster = cluster,
                    onOpenDetail = onOpenDetail,
                    modifier = Modifier.fillMaxWidth()
                )
            }
        }
    }
}

@Composable
private fun SelectedClusterCard(
    cluster: MapCluster,
    onOpenDetail: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(cluster.title, fontWeight = FontWeight.Bold)
            cluster.listings.take(3).forEach { listing ->
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    verticalAlignment = Alignment.Top
                ) {
                    StatusDot(listing.statusKind)
                    Column(Modifier.weight(1f)) {
                        Text(listing.name, fontSize = 13.sp, maxLines = 1, fontWeight = FontWeight.SemiBold)
                        Text(
                            buildList {
                                add(listing.status)
                                listing.displayCity.takeIf { it != "—" }?.let(::add)
                                listing.displayArea.takeIf { it != "—" }?.let(::add)
                                listing.displayAvailableFrom.takeIf { it != "—" }?.let { add("Available $it") }
                            }.joinToString(" · "),
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 2
                        )
                        listing.source.takeIf { it.isNotBlank() }?.let {
                            Text(
                                it,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary
                            )
                        }
                    }
                    Text(listing.displayPrice, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                }
            }
            if (cluster.listings.size > 3) {
                Text("+ ${cluster.listings.size - 3} more nearby", style = MaterialTheme.typography.labelSmall)
            }
            Button(
                onClick = { onOpenDetail(cluster.listings.first().id) },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (cluster.listings.size == 1) "Open listing" else "Open first listing")
            }
        }
    }
}

@Composable
private fun MapFallbackList(
    listings: List<Listing>,
    message: String,
    onOpenDetail: (String) -> Unit
) {
    val cities = listings.groupBy { it.city.ifEmpty { "Unknown" } }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Text(message, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
            Spacer(Modifier.height(12.dp))
            Text("${listings.size} listings with coordinates", style = MaterialTheme.typography.titleMedium)
        }
        cities.forEach { (city, cityListings) ->
            item {
                Card(
                    onClick = { onOpenDetail(cityListings.first().id) },
                    shape = MaterialTheme.shapes.medium,
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)
                ) {
                    Column(Modifier.padding(16.dp)) {
                        Text(city, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(8.dp))
                        cityListings.take(5).forEach { listing ->
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                Text(listing.name, Modifier.weight(1f), fontSize = 13.sp, maxLines = 1)
                                Text(listing.displayPrice, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                            }
                        }
                        if (cityListings.size > 5) {
                            Text("+ ${cityListings.size - 5} more", style = MaterialTheme.typography.labelSmall)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ErrorMapState(message: String, onRetry: () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
            Icon(Icons.Filled.Map, null, Modifier.padding(4.dp), tint = MaterialTheme.colorScheme.error)
            Text(message, color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center)
            Spacer(Modifier.height(12.dp))
            Button(onClick = onRetry) {
                Text("Retry")
            }
        }
    }
}

@Composable
private fun EmptyMapState(message: String) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
            Icon(Icons.Filled.Place, null, Modifier.padding(4.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(message, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun StatusDot(kind: StatusKind) {
    val color = when (kind) {
        StatusKind.BOOK -> StatusBook
        StatusKind.LOTTERY -> StatusLottery
        StatusKind.RESERVED -> StatusReserved
        StatusKind.OTHER -> MaterialTheme.colorScheme.outline
    }
    Box(
        modifier = Modifier
            .padding(top = 5.dp)
            .clip(CircleShape)
            .size(10.dp)
            .background(color)
    )
}

@Composable
private fun ListingMarker(kind: StatusKind) {
    val color = statusColor(kind)
    Surface(
        shape = CircleShape,
        color = color,
        shadowElevation = 3.dp
    ) {
        Box(modifier = Modifier.size(18.dp))
    }
}

@Composable
private fun ClusterMarker(count: Int) {
    Surface(
        shape = CircleShape,
        color = MaterialTheme.colorScheme.primary,
        contentColor = MaterialTheme.colorScheme.onPrimary,
        shadowElevation = 3.dp
    ) {
        Text(
            count.toString(),
            modifier = Modifier.padding(horizontal = 9.dp, vertical = 6.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold
        )
    }
}

@Composable
private fun statusColor(kind: StatusKind) = when (kind) {
    StatusKind.BOOK -> StatusBook
    StatusKind.LOTTERY -> StatusLottery
    StatusKind.RESERVED -> StatusReserved
    StatusKind.OTHER -> MaterialTheme.colorScheme.outline
}

private data class MapCluster(
    val position: LatLng,
    val listings: List<Listing>
) {
    val title: String
        get() = if (listings.size == 1) listings.first().name else "${listings.size} listings"

    val snippet: String
        get() = listings.firstOrNull()?.displayCity.orEmpty()
}

private data class MapListingItem(
    val listing: Listing
) : ClusterItem {
    override fun getPosition(): LatLng = LatLng(listing.latitude ?: 0.0, listing.longitude ?: 0.0)
    override fun getTitle(): String = listing.name
    override fun getSnippet(): String = listing.displayCity
    override fun getZIndex(): Float? = when (listing.statusKind) {
        StatusKind.BOOK -> 4f
        StatusKind.LOTTERY -> 3f
        StatusKind.RESERVED -> 2f
        StatusKind.OTHER -> 1f
    }
}

private fun List<Listing>.toMapItems(): List<MapListingItem> =
    filter { it.latitude != null && it.longitude != null }
        .sortedWith(compareBy<Listing> { it.statusKind.ordinal }.thenBy { it.priceValue ?: Float.MAX_VALUE })
        .map(::MapListingItem)

private fun List<LatLng>.toLatLngBounds(): LatLngBounds {
    val builder = LatLngBounds.builder()
    forEach(builder::include)
    return builder.build()
}
