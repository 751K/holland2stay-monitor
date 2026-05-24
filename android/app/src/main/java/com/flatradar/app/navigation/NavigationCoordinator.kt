package com.flatradar.app.navigation

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Programmatic navigation coordinator.
 *
 * Matches iOS NavigationCoordinator:
 * - Tab switching from deep links / push notification taps
 * - Listing detail push via [openListing]
 * - Auth state reset via [reset]
 */
@Singleton
class NavigationCoordinator @Inject constructor() {

    private val _selectedTab = MutableStateFlow(TopLevelDestination.DASHBOARD)
    val selectedTab = _selectedTab.asStateFlow()

    private val _selectedBrowseMode = MutableStateFlow(BrowseMode.LIST)
    val selectedBrowseMode = _selectedBrowseMode.asStateFlow()

    /** Deep link target listing ID — consumed by ListingsScreen NavHost. */
    private val _pendingListingId = MutableStateFlow<String?>(null)
    val pendingListingId = _pendingListingId.asStateFlow()

    fun selectTab(dest: TopLevelDestination) {
        _selectedTab.value = dest
    }

    fun selectBrowseMode(mode: BrowseMode) {
        _selectedBrowseMode.value = mode
    }

    /**
     * Deep link entry point: switch to Listings tab and push detail.
     * Mirrors iOS coord.openListing(id:titleHint:)
     */
    fun openListing(id: String) {
        // Validate: non-empty, max 128 chars, alphanumeric + -_
        if (id.isEmpty() || id.length > 128) return
        if (!id.all { it.isLetterOrDigit() || it == '-' || it == '_' }) return

        _selectedTab.value = TopLevelDestination.LISTINGS
        _selectedBrowseMode.value = BrowseMode.LIST
        _pendingListingId.value = id
    }

    fun consumePendingListingId(): String? {
        val id = _pendingListingId.value
        _pendingListingId.value = null
        return id
    }

    /**
     * Reset all navigation state on logout / 401.
     * Mirrors iOS NavigationCoordinator.reset()
     */
    fun reset() {
        _selectedTab.value = TopLevelDestination.DASHBOARD
        _selectedBrowseMode.value = BrowseMode.LIST
        _pendingListingId.value = null
    }
}
