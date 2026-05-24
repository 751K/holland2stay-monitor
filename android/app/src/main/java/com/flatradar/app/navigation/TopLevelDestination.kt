package com.flatradar.app.navigation

/**
 * Tab destinations matching iOS AppTab + BrowseMode.
 *
 * iOS: iPhone 4-tab / iPad 6-tab
 * Android: phone NavigationBar 4-tab / tablet NavigationRail 6-tab
 */
enum class TopLevelDestination(
    val route: String,
    val iconDescription: String,
    val adminOnly: Boolean = false
) {
    DASHBOARD(
        route = "dashboard",
        iconDescription = "Dashboard"
    ),
    BROWSE(
        route = "browse",
        iconDescription = "Browse"
    ),
    LISTINGS(
        route = "listings",
        iconDescription = "Listings"
    ),
    MAP(
        route = "map",
        iconDescription = "Map"
    ),
    CALENDAR(
        route = "calendar",
        iconDescription = "Calendar"
    ),
    NOTIFICATIONS(
        route = "notifications",
        iconDescription = "Notifications"
    ),
    SETTINGS(
        route = "settings",
        iconDescription = "Settings"
    );

    companion object {
        /** Phone tabs: Dashboard, Browse (with List/Map/Calendar sub-nav), Notifications, Settings */
        val phoneTabs = listOf(DASHBOARD, BROWSE, NOTIFICATIONS, SETTINGS)

        /** Tablet tabs: all 6 — enough space for full NavigationRail */
        val tabletTabs = listOf(DASHBOARD, LISTINGS, MAP, CALENDAR, NOTIFICATIONS, SETTINGS)
    }
}

/**
 * Browse sub-modes matching iOS BrowseMode {list, map, calendar}.
 */
enum class BrowseMode(val route: String) {
    LIST("browse/list"),
    MAP("browse/map"),
    CALENDAR("browse/calendar")
}
