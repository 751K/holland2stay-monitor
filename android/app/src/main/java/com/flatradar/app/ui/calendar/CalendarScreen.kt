package com.flatradar.app.ui.calendar

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.CalendarMonth
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.domain.model.StatusKind
import com.flatradar.app.ui.theme.StatusBook
import com.flatradar.app.ui.theme.StatusLottery
import com.flatradar.app.ui.theme.StatusReserved
import com.flatradar.app.util.ServerTime
import java.time.DayOfWeek
import java.time.LocalDate
import java.time.YearMonth
import java.time.format.TextStyle
import java.util.Locale

@Composable
fun CalendarScreen(
    onOpenDetail: (String) -> Unit = {},
    viewModel: CalendarViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()

    Scaffold { padding ->
    Box(Modifier.fillMaxSize().padding(padding)) {
    when {
        state.isLoading -> {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        state.errorMessage != null -> {
            CalendarMessageState(
                title = "Unable to load calendar",
                message = state.errorMessage ?: "Network error",
                isError = true,
                onRetry = viewModel::load
            )
        }
        state.listings.isEmpty() -> {
            CalendarMessageState(
                title = "No calendar data",
                message = "Listings with real available dates will appear here.",
                isError = false,
                onRetry = viewModel::load
            )
        }
        else -> {
            val selectedListings = state.selectedDate?.let { state.byDay[it.toString()] }.orEmpty()
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .background(MaterialTheme.colorScheme.surface),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                item {
                    CalendarMonthCard(
                        month = state.visibleMonth,
                        selectedDate = state.selectedDate,
                        byDay = state.byDay,
                        onPrevious = viewModel::previousMonth,
                        onNext = viewModel::nextMonth,
                        onSelect = viewModel::selectDate
                    )
                }
                item {
                    val dateText = state.selectedDate?.let { ServerTime.shortDay(it) }
                        ?: androidx.compose.ui.res.stringResource(com.flatradar.app.R.string.calendar_select_date)
                    val countText = if (state.selectedDate != null) {
                        " · ${selectedListings.size} listing${if (selectedListings.size == 1) "" else "s"}"
                    } else {
                        ""
                    }
                    Text(
                        text = "$dateText$countText",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                }
                if (state.selectedDate == null) {
                    item {
                        Text(
                            text = androidx.compose.ui.res.stringResource(com.flatradar.app.R.string.calendar_select_prompt),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(vertical = 12.dp)
                        )
                    }
                } else if (selectedListings.isEmpty()) {
                    item {
                        Text(
                            "No listings available on this day",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(vertical = 12.dp)
                        )
                    }
                } else {
                    items(selectedListings, key = { it.id }) { listing ->
                        CalendarListingCard(listing = listing, onClick = { onOpenDetail(listing.id) })
                    }
                }
            }
        }
    }
    }  // Box
    }  // Scaffold
}

@Composable
private fun CalendarMonthCard(
    month: YearMonth,
    selectedDate: LocalDate?,
    byDay: Map<String, List<Listing>>,
    onPrevious: () -> Unit,
    onNext: () -> Unit,
    onSelect: (LocalDate) -> Unit
) {
    Card(
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onPrevious) {
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowLeft, "Previous month")
                }
                Text(
                    ServerTime.monthTitle(month.atDay(1)),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = onNext) {
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, "Next month")
                }
            }
            Row(Modifier.fillMaxWidth()) {
                DayOfWeek.entries.forEach { day ->
                    Text(
                        day.getDisplayName(TextStyle.SHORT, Locale.getDefault()).take(2),
                        textAlign = TextAlign.Center,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.weight(1f)
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
            val weeks = remember(month) { monthGrid(month).chunked(7) }
            weeks.forEach { week ->
                Row(Modifier.fillMaxWidth()) {
                    week.forEach { day ->
                        CalendarDayCell(
                            date = day,
                            isInMonth = day.month == month.month && day.year == month.year,
                            isSelected = day == selectedDate,
                            count = byDay[day.toString()]?.size ?: 0,
                            onSelect = { onSelect(day) },
                            modifier = Modifier.weight(1f)
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun CalendarDayCell(
    date: LocalDate,
    isInMonth: Boolean,
    isSelected: Boolean,
    count: Int,
    onSelect: () -> Unit,
    modifier: Modifier = Modifier
) {
    val background = when {
        isSelected -> MaterialTheme.colorScheme.primary
        count > 0 -> MaterialTheme.colorScheme.secondaryContainer
        else -> MaterialTheme.colorScheme.surfaceContainerLowest
    }
    val foreground = when {
        isSelected -> MaterialTheme.colorScheme.onPrimary
        !isInMonth -> MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.45f)
        else -> MaterialTheme.colorScheme.onSurface
    }

    Column(
        modifier = modifier
            .height(54.dp)
            .padding(2.dp)
            .clip(MaterialTheme.shapes.small)
            .background(background)
            .clickable(onClick = onSelect)
            .padding(vertical = 4.dp, horizontal = 2.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text(date.dayOfMonth.toString(), color = foreground, style = MaterialTheme.typography.bodySmall)
        if (count > 0) {
            Spacer(Modifier.height(3.dp))
            Text(
                count.toString(),
                color = foreground,
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold
            )
        }
    }
}

@Composable
private fun CalendarListingCard(listing: Listing, onClick: () -> Unit) {
    Card(
        onClick = onClick,
        shape = MaterialTheme.shapes.medium,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier
                    .clip(CircleShape)
                    .background(listing.statusKind.calendarColor())
                    .padding(5.dp)
            )
            Column(Modifier.padding(start = 12.dp).weight(1f)) {
                Text(listing.name, fontWeight = FontWeight.SemiBold, maxLines = 1)
                val subtitle = if (listing.displayBuilding.isNotEmpty() && listing.displayBuilding != "—") {
                    "${listing.displayCity} · ${listing.displayBuilding}"
                } else {
                    listing.displayCity
                }
                Text(
                    subtitle,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Text(listing.displayPrice, fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun StatusKind.calendarColor() = when (this) {
    StatusKind.BOOK -> StatusBook
    StatusKind.LOTTERY -> StatusLottery
    StatusKind.RESERVED -> StatusReserved
    StatusKind.OTHER -> MaterialTheme.colorScheme.outline
}

@Composable
private fun CalendarMessageState(
    title: String,
    message: String,
    isError: Boolean,
    onRetry: () -> Unit
) {
    val color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.padding(24.dp)
        ) {
            Icon(Icons.Filled.CalendarMonth, null, tint = color)
            Spacer(Modifier.height(8.dp))
            Text(title, color = color, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(4.dp))
            Text(
                message,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                style = MaterialTheme.typography.bodySmall
            )
            Spacer(Modifier.height(12.dp))
            Button(onClick = onRetry) {
                Text("Retry")
            }
        }
    }
}

private fun monthGrid(month: YearMonth): List<LocalDate> {
    val first = month.atDay(1)
    val startOffset = first.dayOfWeek.value - DayOfWeek.MONDAY.value
    val start = first.minusDays(startOffset.toLong())
    return (0 until 42).map { start.plusDays(it.toLong()) }
}
