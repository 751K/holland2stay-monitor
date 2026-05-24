package com.flatradar.app.ui.notifications

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.isSystemInDarkTheme
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Done
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MediumTopAppBar
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.domain.model.NotificationItem
import com.flatradar.app.domain.model.NotificationKind
import com.flatradar.app.ui.theme.StatusBook
import com.flatradar.app.ui.theme.StatusBookContainer
import com.flatradar.app.ui.theme.StatusLottery
import com.flatradar.app.ui.theme.StatusLotteryContainer
import com.flatradar.app.ui.theme.StatusReserved
import com.flatradar.app.ui.theme.StatusReservedContainer
import com.flatradar.app.util.ServerTime
import java.time.LocalDate

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NotificationsScreen(
    onBack: () -> Unit = {},
    onOpenDetail: (String) -> Unit = {},
    viewModel: NotificationsViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val isDark = isSystemInDarkTheme()

    Scaffold(
        topBar = {
            MediumTopAppBar(
                title = { Text("Alerts", fontWeight = FontWeight.Bold) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = {
                    IconButton(onClick = { /* Search */ }) {
                        Icon(Icons.Filled.Search, contentDescription = "Search")
                    }
                    IconButton(onClick = { /* More actions */ }) {
                        Icon(Icons.Filled.MoreVert, contentDescription = "More")
                    }
                }
            )
        },
        floatingActionButton = {
            if (state.unreadCount > 0) {
                ExtendedFloatingActionButton(
                    onClick = { viewModel.markAllRead() },
                    icon = { Icon(Icons.Filled.Done, contentDescription = null) },
                    text = { Text("Mark all read", fontWeight = FontWeight.Bold) },
                    containerColor = if (isDark) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.primaryContainer,
                    contentColor = if (isDark) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onPrimaryContainer,
                    shape = CircleShape
                )
            }
        }
    ) { padding ->
        when {
            state.isLoading -> {
                Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
            state.items.isEmpty() -> {
                Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                    Text("No notifications", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            else -> {
                var selectedFilter by remember { mutableStateOf("All") }

                val todayCount = remember(state.items) {
                    state.items.count { item ->
                        ServerTime.parseInstant(item.createdAt)
                            ?.atZone(ServerTime.zone)
                            ?.toLocalDate() == LocalDate.now(ServerTime.zone)
                    }
                }

                val filteredItems = remember(state.items, selectedFilter) {
                    if (selectedFilter == "All") {
                        state.items
                    } else {
                        state.items.filter { item ->
                            val kind = NotificationKind.classify(item)
                            when (selectedFilter) {
                                "Book" -> kind == NotificationKind.BOOK
                                "Lottery" -> kind == NotificationKind.LOTTERY
                                "Status" -> kind == NotificationKind.STATUS
                                "System" -> kind == NotificationKind.SYSTEM || kind == NotificationKind.ALERT || kind == NotificationKind.TEST
                                else -> true
                            }
                        }
                    }
                }

                val groups = notificationGroups(filteredItems)

                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding)
                ) {
                    // 1. Stats Capsule Row
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        // Left Capsule Pill
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .background(
                                    if (isDark) Color(0xFF0D2D2D)
                                    else Color(0xFFE2F9F6)
                                )
                                .padding(horizontal = 10.dp, vertical = 6.dp)
                        ) {
                            Box(
                                modifier = Modifier
                                    .size(6.dp)
                                    .clip(CircleShape)
                                    .background(Color(0xFF00BFA5))
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(
                                text = "${todayCount} today · updated 5s ago",
                                style = MaterialTheme.typography.labelMedium,
                                color = if (isDark) Color(0xFF4EE2D0) else Color(0xFF006057),
                                fontWeight = FontWeight.SemiBold
                            )
                        }

                        // Right Unread text
                        Row(
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Box(
                                modifier = Modifier
                                    .size(6.dp)
                                    .clip(CircleShape)
                                    .background(MaterialTheme.colorScheme.primary)
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(
                                text = "${state.unreadCount} unread",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.primary,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }

                    // 2. Filter Chips
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .horizontalScroll(rememberScrollState())
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        AlertFilterChip(
                            text = "All · ${state.items.size}",
                            selected = selectedFilter == "All",
                            onClick = { selectedFilter = "All" }
                        )
                        AlertFilterChip(
                            text = "Book",
                            selected = selectedFilter == "Book",
                            onClick = { selectedFilter = "Book" }
                        )
                        AlertFilterChip(
                            text = "Lottery",
                            selected = selectedFilter == "Lottery",
                            onClick = { selectedFilter = "Lottery" }
                        )
                        AlertFilterChip(
                            text = "Status",
                            selected = selectedFilter == "Status",
                            onClick = { selectedFilter = "Status" }
                        )
                        AlertFilterChip(
                            text = "System",
                            selected = selectedFilter == "System",
                            onClick = { selectedFilter = "System" }
                        )
                    }

                    // 3. LazyColumn list
                    LazyColumn(
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(bottom = 88.dp) // Reserve space for extended FAB
                    ) {
                        groups.forEach { group ->
                            if (group.items.isNotEmpty()) {
                                item(key = "header-${group.title}") {
                                    Text(
                                        text = "${group.title} · ${group.items.size}",
                                        style = MaterialTheme.typography.labelMedium,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                                    )
                                }
                                items(group.items, key = { it.id }) { notification ->
                                    SwipeToReadRow(
                                        notification = notification,
                                        onClick = {
                                            if (!notification.isRead) viewModel.markRead(notification.id)
                                            if (notification.listingId.isNotEmpty()) onOpenDetail(notification.listingId)
                                        },
                                        onMarkRead = { viewModel.markRead(notification.id) }
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun AlertFilterChip(
    text: String,
    selected: Boolean,
    onClick: () -> Unit
) {
    Surface(
        onClick = onClick,
        shape = RoundedCornerShape(8.dp),
        border = if (selected) null else BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        color = if (selected) MaterialTheme.colorScheme.primaryContainer else Color.Transparent,
        contentColor = if (selected) MaterialTheme.colorScheme.onPrimaryContainer else MaterialTheme.colorScheme.onSurface,
        modifier = Modifier.height(32.dp)
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.Center
        ) {
            if (selected) {
                Icon(
                    imageVector = Icons.Filled.Done,
                    contentDescription = null,
                    modifier = Modifier.size(16.dp)
                )
                Spacer(modifier = Modifier.width(4.dp))
            }
            Text(
                text = text,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.Medium
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SwipeToReadRow(
    notification: NotificationItem,
    onClick: () -> Unit,
    onMarkRead: () -> Unit
) {
    if (notification.isRead) {
        NotificationRow(notification = notification, onClick = onClick, onMarkRead = onMarkRead)
        return
    }

    val dismissState = rememberSwipeToDismissBoxState(
        confirmValueChange = { value ->
            if (value != SwipeToDismissBoxValue.Settled) {
                onMarkRead()
                true
            } else {
                false
            }
        }
    )

    LaunchedEffect(notification.isRead) {
        if (notification.isRead) dismissState.reset()
    }

    SwipeToDismissBox(
        state = dismissState,
        backgroundContent = {
            Row(
                modifier = Modifier
                    .fillMaxSize()
                    .background(MaterialTheme.colorScheme.primary)
                    .padding(horizontal = 20.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.End
            ) {
                Icon(Icons.Filled.Done, "Mark read", tint = MaterialTheme.colorScheme.onPrimary)
                Spacer(Modifier.width(8.dp))
                Text("Mark read", color = MaterialTheme.colorScheme.onPrimary)
            }
        },
        content = {
            NotificationRow(notification = notification, onClick = onClick, onMarkRead = onMarkRead)
        }
    )
}

@Composable
private fun NotificationRow(
    notification: NotificationItem,
    onClick: () -> Unit,
    onMarkRead: () -> Unit
) {
    val kind = NotificationKind.classify(notification)
    val isDark = isSystemInDarkTheme()
    val (badgeBgColor, iconColor) = getKindColors(kind, isDark)
    var menuExpanded by remember { mutableStateOf(false) }

    // Tag text formatting
    val tagText = when (kind) {
        NotificationKind.BOOK -> "NEW · BOOK"
        NotificationKind.LOTTERY -> "NEW · LOTTERY"
        NotificationKind.STATUS -> "STATUS CHANGE"
        NotificationKind.ALERT -> "SYSTEM"
        NotificationKind.TEST -> "TEST"
        NotificationKind.SYSTEM -> "SYSTEM"
    }

    // Flat background depending on read status
    val rowBgColor = if (!notification.isRead) {
        if (isDark) Color(0xFF1E293B).copy(alpha = 0.5f)
        else Color(0xFFF4F6FA)
    } else {
        Color.Transparent
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(rowBgColor)
            .clickable(onClick = onClick)
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 16.dp),
            verticalAlignment = Alignment.Top
        ) {
            // Overlapping Badge Box
            Box(modifier = Modifier.size(48.dp)) {
                Box(
                    modifier = Modifier
                        .size(42.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .background(badgeBgColor)
                        .align(Alignment.BottomStart),
                    contentAlignment = Alignment.Center
                ) {
                    when (kind) {
                        NotificationKind.BOOK -> {
                            Icon(
                                imageVector = Icons.Filled.Home,
                                contentDescription = null,
                                tint = iconColor,
                                modifier = Modifier.size(22.dp)
                            )
                        }
                        NotificationKind.LOTTERY -> {
                            Icon(
                                imageVector = Icons.Filled.Star,
                                contentDescription = null,
                                tint = iconColor,
                                modifier = Modifier.size(22.dp)
                            )
                        }
                        NotificationKind.STATUS -> {
                            Icon(
                                imageVector = Icons.Filled.Refresh,
                                contentDescription = null,
                                tint = iconColor,
                                modifier = Modifier.size(22.dp)
                            )
                        }
                        else -> {
                            // Custom circle outline for System/Alert
                            Box(
                                modifier = Modifier
                                    .size(20.dp)
                                    .border(2.dp, iconColor, CircleShape)
                            )
                        }
                    }
                }
                if (!notification.isRead) {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .clip(CircleShape)
                            .background(MaterialTheme.colorScheme.primary)
                            .border(1.5.dp, if (isDark) Color(0xFF0F172A) else Color.White, CircleShape)
                            .align(Alignment.TopEnd)
                    )
                }
            }

            Spacer(modifier = Modifier.width(12.dp))

            // Notification Info Content
            Column(modifier = Modifier.weight(1f)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = tagText,
                        style = MaterialTheme.typography.labelSmall,
                        color = iconColor,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = ServerTime.relative(notification.createdAt),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Spacer(modifier = Modifier.height(4.dp))
                
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = notification.title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.weight(1f)
                    )
                    Box {
                        IconButton(onClick = { menuExpanded = true }, modifier = Modifier.size(24.dp)) {
                            Icon(
                                imageVector = Icons.Filled.MoreVert,
                                contentDescription = "Notification Actions",
                                tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                            )
                        }
                        DropdownMenu(
                            expanded = menuExpanded,
                            onDismissRequest = { menuExpanded = false }
                        ) {
                            if (notification.listingId.isNotEmpty()) {
                                DropdownMenuItem(
                                    text = { Text("Open listing") },
                                    onClick = {
                                        menuExpanded = false
                                        onClick()
                                    }
                                )
                            }
                            if (!notification.isRead) {
                                DropdownMenuItem(
                                    text = { Text("Mark read") },
                                    onClick = {
                                        menuExpanded = false
                                        onMarkRead()
                                    }
                                )
                            }
                        }
                    }
                }
                Spacer(modifier = Modifier.height(4.dp))
                
                // Normalizing body text arrows (-> to →)
                val bodyText = notification.body.replace("->", "→")
                Text(
                    text = bodyText,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2
                )
            }
        }
        HorizontalDivider(
            modifier = Modifier.fillMaxWidth(),
            color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.5f)
        )
    }
}

private fun getKindColors(kind: NotificationKind, isDark: Boolean): Pair<Color, Color> {
    return when (kind) {
        NotificationKind.BOOK -> {
            if (isDark) Color(0xFF1B5E20) to Color(0xFF81C784)
            else Color(0xFFE8F5E9) to Color(0xFF2E7D32)
        }
        NotificationKind.LOTTERY -> {
            if (isDark) Color(0xFF5D4037) to Color(0xFFD7CCC8)
            else Color(0xFFFFF3E0) to Color(0xFFE65100)
        }
        NotificationKind.STATUS -> {
            if (isDark) Color(0xFF0D47A1) to Color(0xFF90CAF9)
            else Color(0xFFE3F2FD) to Color(0xFF1565C0)
        }
        NotificationKind.ALERT -> {
            if (isDark) Color(0xFFB71C1C) to Color(0xFFFF8A80)
            else Color(0xFFFFEBEE) to Color(0xFFC62828)
        }
        NotificationKind.TEST -> {
            if (isDark) Color(0xFF4A148C) to Color(0xFFE1BEE7)
            else Color(0xFFF3E5F5) to Color(0xFF6A1B9A)
        }
        NotificationKind.SYSTEM -> {
            if (isDark) Color(0xFF212121) to Color(0xFFBDBDBD)
            else Color(0xFFF5F5F5) to Color(0xFF616161)
        }
    }
}

private data class NotificationGroup(
    val title: String,
    val items: List<NotificationItem>
)

private fun notificationGroups(items: List<NotificationItem>): List<NotificationGroup> {
    val today = LocalDate.now(ServerTime.zone)
    val yesterday = today.minusDays(1)
    val byBucket = items.groupBy { item ->
        when (ServerTime.parseInstant(item.createdAt)?.atZone(ServerTime.zone)?.toLocalDate()) {
            today -> "TODAY"
            yesterday -> "YESTERDAY"
            else -> "EARLIER"
        }
    }
    return listOf(
        NotificationGroup("TODAY", byBucket["TODAY"].orEmpty()),
        NotificationGroup("YESTERDAY", byBucket["YESTERDAY"].orEmpty()),
        NotificationGroup("EARLIER", byBucket["EARLIER"].orEmpty())
    )
}
