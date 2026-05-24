package com.flatradar.app.ui.dashboard

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Home
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.domain.model.MonitorStatus
import com.flatradar.app.domain.model.StatusKind
import com.flatradar.app.domain.model.ChartData
import com.flatradar.app.domain.model.ChartEntry
import com.flatradar.app.domain.model.bucketed
import com.flatradar.app.data.remote.MeSummaryResponse
import com.flatradar.app.ui.theme.*

/**
 * Dashboard screen — matches Material 3 Design Specifications.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    isUser: Boolean = false,
    userName: String? = null,
    onOpenListing: (String) -> Unit = {},
    onNavigateToBrowse: () -> Unit = {},
    onLogout: () -> Unit = {},
    viewModel: DashboardViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    var selectedChart by remember { mutableStateOf<ChartDetailContent?>(null) }

    LaunchedEffect(Unit) {
        viewModel.fetchAll(isUser)
    }

    selectedChart?.let { chart ->
        ModalBottomSheet(onDismissRequest = { selectedChart = null }) {
            ChartDetailSheet(
                chart = chart,
                onDismiss = { selectedChart = null }
            )
        }
    }

    Scaffold(
        contentWindowInsets = WindowInsets(0.dp),
        containerColor = MaterialTheme.colorScheme.background
    ) { padding ->
        if (state.isLoading && state.summary == null) {
            Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator()
            }
        } else if (state.errorMessage != null && state.summary == null) {
            Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(state.errorMessage ?: "Unable to Load", color = MaterialTheme.colorScheme.error)
                    Spacer(modifier = Modifier.height(12.dp))
                    Button(onClick = { viewModel.fetchAll(isUser) }) { Text("Try Again") }
                }
            }
        } else {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding)
                    .statusBarsPadding()
                    .verticalScroll(rememberScrollState())
            ) {
                // 1. Header
                DashboardHeader(userName, onLogout)

                Spacer(modifier = Modifier.height(8.dp))

                // 2. Quick Stats (Horizontal Carousel)
                QuickStatsCarousel(
                    summary = state.summary,
                    meSummary = state.meSummary,
                    onNavigateToBrowse = onNavigateToBrowse
                )
                Spacer(modifier = Modifier.height(24.dp))

                // 3. Your matches (user only)
                val meSummary = state.meSummary
                if (isUser && meSummary != null) {
                    MatchesSection(
                        meSummary = meSummary,
                        previews = state.matchPreviews,
                        onOpenListing = onOpenListing,
                        onSeeAll = onNavigateToBrowse
                    )
                    Spacer(modifier = Modifier.height(24.dp))
                }

                // 4. Explore section
                ExploreSection(
                    state = state,
                    onOpenChart = { selectedChart = it }
                )
                
                Spacer(modifier = Modifier.height(32.dp))
            }
        }
    }
}

// ── Quick Stats Carousel ──────────────────────────────────────────

@Composable
private fun QuickStatsCarousel(
    summary: MonitorStatus?,
    meSummary: MeSummaryResponse?,
    onNavigateToBrowse: () -> Unit
) {
    val items = remember(summary, meSummary) {
        listOfNotNull(
            StatItem("Active Listings", summary?.total?.toString() ?: "—", Icons.Filled.Home),
            meSummary?.let { StatItem("Matched", it.matchedTotal.toString(), Icons.Filled.Search) },
            StatItem("Alerts Today", summary?.new24h?.toString() ?: "—", Icons.Filled.Notifications)
        )
    }

    LazyRow(
        modifier = Modifier.fillMaxWidth(),
        contentPadding = PaddingValues(horizontal = 20.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        items(items) { item ->
            QuickStatCard(item, onClick = onNavigateToBrowse)
        }
    }
}

private data class StatItem(val title: String, val value: String, val icon: ImageVector)

@Composable
private fun QuickStatCard(item: StatItem, onClick: () -> Unit) {
    Card(
        onClick = onClick,
        modifier = Modifier.width(160.dp),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerLow
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Icon(
                item.icon,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(24.dp)
            )
            Spacer(modifier = Modifier.height(12.dp))
            Text(item.value, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(item.title, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}


// ── Header ────────────────────────────────────────────────────────

@Composable
private fun DashboardHeader(
    userName: String?,
    onLogout: () -> Unit,
    modifier: Modifier = Modifier
) {
    val greeting = remember(userName) {
        val hour = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY)
        val prefix = when (hour) {
            in 5..11 -> "Good morning"
            in 12..16 -> "Good afternoon"
            else -> "Good evening"
        }
        if (userName.isNullOrEmpty()) prefix else "$prefix, $userName"
    }

    var menuExpanded by remember { mutableStateOf(false) }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 20.dp, vertical = 12.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text("Dashboard", style = MaterialTheme.typography.headlineMedium)
            Text(greeting, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Box {
            IconButton(onClick = { menuExpanded = true }) {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.Logout,
                    contentDescription = "Log Out Option",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            DropdownMenu(
                expanded = menuExpanded,
                onDismissRequest = { menuExpanded = false }
            ) {
                DropdownMenuItem(
                    text = { Text("Log Out", color = MaterialTheme.colorScheme.error) },
                    onClick = {
                        menuExpanded = false
                        onLogout()
                    },
                    leadingIcon = {
                        Icon(
                            imageVector = Icons.AutoMirrored.Filled.Logout,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.error
                        )
                    }
                )
            }
        }
    }
}

// ── Live Badge ────────────────────────────────────────────────────

@Composable
private fun LiveBadge(summary: MonitorStatus, isStale: Boolean) {
    val dotColor = if (isStale) StatusLottery else StatusBook
    val statusText = if (isStale) "Offline" else "Live"
    val relativeTime = summary.lastScrape.let { iso ->
        // Simple relative time: "just now" / "Xm ago" / "Xh ago" / "Xd ago"
        "updated just now" // TODO: proper relative time helper
    }

    Row(
        modifier = Modifier
            .padding(horizontal = 20.dp)
            .clip(RoundedCornerShape(24.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 14.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(dotColor)
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(statusText, style = MaterialTheme.typography.bodySmall, color = if (isStale) StatusLottery else MaterialTheme.colorScheme.onSurface)
        Text(" · ", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(relativeTime, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ── Stats Card ─────────────────────────────────────────────────────

@Composable
private fun StatsCard(
    summary: MonitorStatus,
    chartDailyNew: ChartData?,
    onNew24hTap: () -> Unit,
    onNew7dTap: () -> Unit,
    onChangesTap: () -> Unit
) {
    Card(
        modifier = Modifier.padding(horizontal = 16.dp),
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer,
            contentColor = MaterialTheme.colorScheme.onPrimaryContainer
        )
    ) {
        Column {
            Row(
                modifier = Modifier.padding(20.dp),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Column {
                    Text(
                        "LIVE · TRACKING",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.78f)
                    )
                    Text(
                        "${summary.total}",
                        style = MaterialTheme.typography.displayMedium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                    if (summary.new7d > 0) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("↑", color = StatusOnBookContainer, fontWeight = FontWeight.Bold)
                            Text(
                                "+${summary.new7d} this week",
                                style = MaterialTheme.typography.bodySmall,
                                color = StatusOnBookContainer
                            )
                        }
                    }
                }
                Spacer(modifier = Modifier.width(16.dp))
                Box(
                    modifier = Modifier
                        .width(130.dp)
                        .height(70.dp)
                        .background(
                            MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.08f),
                            MaterialTheme.shapes.small
                        ),
                    contentAlignment = Alignment.Center
                ) {
                    Text("updated", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.72f))
                }
            }

            HorizontalDivider(
                modifier = Modifier.padding(horizontal = 20.dp),
                color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.18f)
            )

            // Bottom: 3 mini stats
            Row(
                modifier = Modifier
                    .padding(vertical = 14.dp, horizontal = 20.dp)
                    .fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                MiniStat(
                    num = summary.new24h,
                    label = "New · 24h",
                    onClick = onNew24hTap,
                    modifier = Modifier.weight(1f)
                )
                VerticalDivider(modifier = Modifier.padding(horizontal = 5.dp))
                MiniStat(
                    num = summary.new7d,
                    label = "New · 7d",
                    onClick = onNew7dTap,
                    modifier = Modifier.weight(1f)
                )
                VerticalDivider(modifier = Modifier.padding(horizontal = 5.dp))
                MiniStat(
                    num = summary.changes24h,
                    label = "Changes",
                    onClick = onChangesTap,
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

@Composable
private fun MiniStat(num: Int, label: String, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.clickable(onClick = onClick)
    ) {
        Text(
            "$num",
            style = MaterialTheme.typography.headlineSmall,
            color = MaterialTheme.colorScheme.onPrimaryContainer
        )
        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.78f))
    }
}

// ── Matches Section ────────────────────────────────────────────────

@Composable
private fun MatchesSection(
    meSummary: MeSummaryResponse,
    previews: List<Listing>,
    onOpenListing: (String) -> Unit,
    onSeeAll: () -> Unit
) {
    Column {
        Row(
            modifier = Modifier.padding(horizontal = 20.dp),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text("Your matches", style = MaterialTheme.typography.headlineMedium)
            TextButton(onClick = onSeeAll) {
                Text("See all ${meSummary.matchedTotal}")
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Preview cards
        Row(
            modifier = Modifier
                .padding(horizontal = 20.dp)
                .fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            val previewCount = minOf(previews.size, 3)
            if (previewCount == 0) {
                repeat(3) {
                    MatchPreviewPlaceholder(modifier = Modifier.weight(1f))
                }
            } else {
                previews.take(3).forEach { listing ->
                    MatchPreviewCard(
                        listing = listing,
                        onClick = { onOpenListing(listing.id) },
                        modifier = Modifier.weight(1f)
                    )
                }
                repeat(3 - previewCount) {
                    MatchPreviewPlaceholder(modifier = Modifier.weight(1f))
                }
            }
        }
    }
}

@Composable
private fun MatchPreviewCard(listing: Listing, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Card(
        onClick = onClick,
        modifier = modifier.height(128.dp),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text(listing.displayPrice, fontWeight = FontWeight.Bold, fontSize = 13.sp)
            Spacer(modifier = Modifier.height(4.dp))
            StatusBadge(listing.statusKind, listing.status)
            listing.displayArea.takeIf { it != "—" }?.let {
                Spacer(modifier = Modifier.height(2.dp))
                Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            listing.displayCity.takeIf { it != "—" }?.let {
                Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            listing.displayAvailableFrom.takeIf { it != "—" }?.let {
                Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun MatchPreviewPlaceholder(modifier: Modifier = Modifier) {
    Card(
        modifier = modifier.height(128.dp),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
    ) {
        Box(
            modifier = Modifier.fillMaxSize().padding(12.dp),
            contentAlignment = Alignment.Center
        ) {
            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

// ── Status Badge ───────────────────────────────────────────────────

@Composable
fun StatusBadge(kind: StatusKind, status: String) {
    val dotColor = when (kind) {
        StatusKind.BOOK -> StatusBook
        StatusKind.LOTTERY -> StatusLottery
        StatusKind.RESERVED -> StatusReserved
        StatusKind.OTHER -> StatusReserved
    }
    val containerColor = when (kind) {
        StatusKind.BOOK -> StatusBookContainer
        StatusKind.LOTTERY -> StatusLotteryContainer
        StatusKind.RESERVED -> StatusReservedContainer
        StatusKind.OTHER -> StatusReservedContainer
    }
    val textColor = when (kind) {
        StatusKind.BOOK -> StatusOnBookContainer
        StatusKind.LOTTERY -> StatusOnLotteryContainer
        StatusKind.RESERVED -> StatusOnReservedContainer
        StatusKind.OTHER -> StatusOnReservedContainer
    }
    val label = when (kind) {
        StatusKind.BOOK -> "Available"
        StatusKind.LOTTERY -> "Lottery"
        StatusKind.RESERVED -> "Reserved"
        StatusKind.OTHER -> status
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(containerColor)
            .padding(horizontal = 6.dp, vertical = 3.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(modifier = Modifier.size(5.dp).clip(CircleShape).background(dotColor))
        Spacer(modifier = Modifier.width(4.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = textColor,
            maxLines = 1
        )
    }
}

// ── Explore Section ────────────────────────────────────────────────

@Composable
private fun ExploreSection(
    state: DashboardViewModel.DashboardUiState,
    onOpenChart: (ChartDetailContent) -> Unit
) {
    val sourceEntries = remember(state.chartSource) {
        state.chartSource?.data.orEmpty().bucketed("source_dist")
    }
    val statusEntries = remember(state.chartStatus) {
        statusBuckets(state.chartStatus?.data.orEmpty())
    }
    val priceEntries = remember(state.chartPrice) {
        state.chartPrice?.data.orEmpty().sortedBy { priceSortKey(it.label) }
    }
    val typeEntries = remember(state.chartType) {
        state.chartType?.data.orEmpty()
            .bucketed("type_dist")
            .sortedByDescending { it.count }
            .take(3)
    }
    val energyEntries = remember(state.chartEnergy) {
        state.chartEnergy?.data.orEmpty().bucketed("energy_dist")
    }
    val tenantEntries = remember(state.chartTenant) {
        state.chartTenant?.data.orEmpty()
            .sortedByDescending { it.count }
            .take(3)
    }

    Column {
        Text(
            "Explore",
            style = MaterialTheme.typography.headlineMedium,
            modifier = Modifier.padding(horizontal = 20.dp)
        )
        Spacer(modifier = Modifier.height(12.dp))

        Column(
            modifier = Modifier.padding(horizontal = 20.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ExploreMiniCard(
                    title = "By platform",
                    modifier = Modifier.weight(1f),
                    enabled = sourceEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Platform", "source_dist", sourceEntries)) },
                    content = {
                        if (sourceEntries.isNotEmpty()) {
                            DistributionBar(
                                entries = sourceEntries,
                                colors = listOf(StatusBook, StatusLottery, StatusReserved)
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            CompactLegend(sourceEntries, listOf(StatusBook, StatusLottery, StatusReserved), maxItems = 3)
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
                ExploreMiniCard(
                    title = "By status",
                    modifier = Modifier.weight(1f),
                    enabled = statusEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Status", "status_dist", statusEntries)) },
                    content = {
                        if (statusEntries.isNotEmpty()) {
                            DistributionBar(
                                entries = statusEntries,
                                colors = listOf(StatusBook, StatusLottery, StatusReserved.copy(alpha = 0.55f))
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            CompactLegend(
                                entries = statusEntries,
                                colors = listOf(StatusBook, StatusLottery, StatusReserved),
                                maxItems = 3,
                                labelMapper = { if (it == "available") "book" else if (it == "unavailable") "other" else it }
                            )
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ExploreMiniCard(
                    title = "By price",
                    modifier = Modifier.weight(1f),
                    enabled = priceEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Price", "price_dist", priceEntries)) },
                    content = {
                        if (priceEntries.isNotEmpty()) {
                            VerticalBars(priceEntries.take(9), MaterialTheme.colorScheme.primary)
                            Spacer(modifier = Modifier.height(5.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                Text(priceEntries.first().label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                Spacer(modifier = Modifier.weight(1f))
                                Text(priceEntries.last().label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
                ExploreMiniCard(
                    title = "By type",
                    modifier = Modifier.weight(1f),
                    enabled = typeEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Type", "type_dist", typeEntries)) },
                    content = {
                        if (typeEntries.isNotEmpty()) {
                            HorizontalBars(typeEntries)
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ExploreMiniCard(
                    title = "By energy",
                    modifier = Modifier.weight(1f),
                    enabled = energyEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Energy", "energy_dist", energyEntries)) },
                    content = {
                        if (energyEntries.isNotEmpty()) {
                            VerticalBars(
                                entries = energyEntries,
                                colorProvider = { energyColor(it.label) }
                            )
                            Spacer(modifier = Modifier.height(5.dp))
                            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                energyEntries.take(5).forEach {
                                    Text(
                                        it.label,
                                        modifier = Modifier.weight(1f),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        fontWeight = FontWeight.Bold
                                    )
                                }
                            }
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
                ExploreMiniCard(
                    title = "By tenant",
                    modifier = Modifier.weight(1f),
                    enabled = tenantEntries.isNotEmpty(),
                    onClick = { onOpenChart(ChartDetailContent("By Tenant", "tenant_dist", tenantEntries)) },
                    content = {
                        if (tenantEntries.isNotEmpty()) {
                            HorizontalBars(tenantEntries, labelMapper = ::tenantMiniLabel)
                        } else {
                            Text("—", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                )
            }
        }
    }
}

@Composable
private fun DistributionBar(entries: List<ChartEntry>, colors: List<Color>, modifier: Modifier = Modifier) {
    val total = entries.sumOf { it.count }.coerceAtLeast(1)
    Row(modifier = modifier.fillMaxWidth().height(6.dp).clip(CircleShape)) {
        entries.forEachIndexed { index, entry ->
            if (entry.count > 0) {
                Box(
                    modifier = Modifier
                        .weight(entry.count.toFloat() / total)
                        .fillMaxHeight()
                        .background(colors[index % colors.size])
                )
            }
        }
    }
}

@Composable
private fun CompactLegend(
    entries: List<ChartEntry>,
    colors: List<Color>,
    maxItems: Int,
    labelMapper: (String) -> String = { it }
) {
    Row(modifier = Modifier.fillMaxWidth()) {
        entries.take(maxItems).forEachIndexed { index, entry ->
            Column(
                modifier = Modifier.weight(1f),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(
                    "${entry.count}",
                    style = MaterialTheme.typography.labelMedium,
                    fontWeight = FontWeight.Bold,
                    color = colors[index % colors.size]
                )
                Text(
                    labelMapper(entry.label),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
private fun VerticalBars(entries: List<ChartEntry>, color: Color) {
    VerticalBars(entries = entries, colorProvider = { color })
}

@Composable
private fun VerticalBars(entries: List<ChartEntry>, colorProvider: (ChartEntry) -> Color) {
    val maxCount = entries.maxOfOrNull { it.count }?.coerceAtLeast(1) ?: 1
    Row(
        modifier = Modifier.fillMaxWidth().height(36.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
        verticalAlignment = Alignment.Bottom
    ) {
        entries.forEach { entry ->
            val ratio = ratio(entry.count, maxCount)
            Box(
                modifier = Modifier.weight(1f).fillMaxHeight(),
                contentAlignment = Alignment.BottomCenter
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth(0.72f)
                        .height(maxOf(4.dp, 36.dp * ratio))
                        .clip(RoundedCornerShape(2.dp))
                        .background(colorProvider(entry).copy(alpha = 0.68f))
                )
            }
        }
    }
}

@Composable
private fun HorizontalBars(
    entries: List<ChartEntry>,
    labelMapper: (String) -> String = { it }
) {
    val maxCount = entries.maxOfOrNull { it.count }?.coerceAtLeast(1) ?: 1
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        entries.forEach { entry ->
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(
                    labelMapper(entry.label),
                    modifier = Modifier.width(50.dp),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .height(5.dp)
                        .clip(CircleShape)
                        .background(MaterialTheme.colorScheme.surfaceVariant)
                ) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth(ratio(entry.count, maxCount))
                            .fillMaxHeight()
                            .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.68f))
                    )
                }
                Text(
                    "${entry.count}",
                    modifier = Modifier.width(24.dp),
                    style = MaterialTheme.typography.labelSmall,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onSurface
                )
            }
        }
    }
}

private fun statusBuckets(entries: List<ChartEntry>): List<ChartEntry> {
    if (entries.isEmpty()) return emptyList()
    val total = entries.sumOf { it.count }
    val available = entries.firstOrNull { it.label.lowercase().contains("available") }?.count ?: 0
    val lottery = entries.firstOrNull { it.label.lowercase().contains("lottery") }?.count ?: 0
    val unavailable = (total - available - lottery).coerceAtLeast(0)
    return listOf(
        ChartEntry(rawLabel = "available", count = available),
        ChartEntry(rawLabel = "lottery", count = lottery),
        ChartEntry(rawLabel = "unavailable", count = unavailable)
    ).filter { it.count > 0 }
}

private fun priceSortKey(label: String): Int {
    return when {
        label.startsWith("<") -> 0
        label.startsWith(">") -> Int.MAX_VALUE
        else -> Regex("\\d+").find(label)?.value?.toIntOrNull() ?: Int.MAX_VALUE - 1
    }
}

private fun tenantMiniLabel(label: String): String {
    val lower = label.lowercase()
    return when {
        lower.contains("student") -> "Student"
        lower.contains("working") || lower.contains("professional") -> "Working"
        lower.contains("couple") -> "Couple"
        else -> label.substringBefore("(").trim().ifBlank { label }
    }
}

private fun energyColor(label: String): Color {
    return when (label.uppercase()) {
        "A+" -> EnergyAPlus
        "A" -> EnergyA
        "B" -> StatusBook
        "C" -> PlatformXior
        "D", "E" -> StatusLottery
        else -> StatusReserved
    }
}

private fun chartColor(key: String, label: String): Color {
    return when (key) {
        "status_dist" -> when (label.lowercase()) {
            "available" -> StatusBook
            "lottery" -> StatusLottery
            else -> StatusReserved
        }
        "source_dist" -> when (label) {
            "H2S" -> PlatformHolland2Stay
            "OD" -> PlatformOurDomain
            "XR" -> PlatformXior
            else -> StatusReserved
        }
        "energy_dist" -> energyColor(label)
        else -> Blue500
    }
}

private fun ratio(value: Int, maxValue: Int): Float {
    if (maxValue <= 0) return 0f
    return (value.toFloat() / maxValue.toFloat()).coerceIn(0.04f, 1f)
}

private data class ChartDetailContent(
    val title: String,
    val key: String,
    val entries: List<ChartEntry>
)

@Composable
private fun ChartDetailSheet(
    chart: ChartDetailContent,
    onDismiss: () -> Unit
) {
    val total = chart.entries.sumOf { it.count }
    val sortedEntries = remember(chart) {
        when (chart.key) {
            "price_dist" -> chart.entries.sortedBy { priceSortKey(it.label) }
            "energy_dist" -> chart.entries
            else -> chart.entries.sortedByDescending { it.count }
        }
    }
    val maxCount = sortedEntries.maxOfOrNull { it.count }?.coerceAtLeast(1) ?: 1

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .navigationBarsPadding()
            .padding(horizontal = 20.dp)
            .padding(bottom = 24.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text(chart.title, style = MaterialTheme.typography.headlineSmall)
                Text(
                    "$total listings",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            IconButton(onClick = onDismiss) {
                Icon(Icons.Filled.Close, contentDescription = "Close")
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        if (sortedEntries.isEmpty()) {
            Text("No data", color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 520.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                sortedEntries.forEach { entry ->
                    ChartDetailRow(
                        entry = entry,
                        maxCount = maxCount,
                        color = chartColor(chart.key, entry.label)
                    )
                }
            }
        }
    }
}

@Composable
private fun ChartDetailRow(
    entry: ChartEntry,
    maxCount: Int,
    color: Color
) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                entry.label,
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                "${entry.count}",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Bold
            )
        }
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(8.dp)
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth(ratio(entry.count, maxCount))
                    .fillMaxHeight()
                    .background(color)
            )
        }
    }
}

@Composable
private fun ExploreMiniCard(
    title: String,
    modifier: Modifier = Modifier,
    enabled: Boolean,
    onClick: () -> Unit,
    content: @Composable () -> Unit
) {
    Card(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.height(116.dp),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(title, style = MaterialTheme.typography.titleSmall)
                Text("›", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(modifier = Modifier.height(10.dp))
            content()
        }
    }
}
