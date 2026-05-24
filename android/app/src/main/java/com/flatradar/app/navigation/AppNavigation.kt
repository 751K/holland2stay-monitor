package com.flatradar.app.navigation

import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ViewList
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.data.local.AppPreferences
import com.flatradar.app.ui.auth.LoginScreen
import com.flatradar.app.ui.calendar.CalendarScreen
import com.flatradar.app.ui.dashboard.DashboardScreen
import com.flatradar.app.ui.listings.ListingDetailScreen
import com.flatradar.app.ui.listings.ListingsScreen
import com.flatradar.app.ui.map.MapScreen
import com.flatradar.app.ui.notifications.NotificationsScreen
import com.flatradar.app.ui.notifications.NotificationsViewModel
import com.flatradar.app.ui.settings.FilterEditScreen
import com.flatradar.app.ui.settings.FeedbackScreen
import com.flatradar.app.ui.settings.LegalScreen
import com.flatradar.app.ui.settings.LegalText
import com.flatradar.app.ui.settings.SettingsScreen
import com.flatradar.app.ui.admin.AdminMonitorScreen
import com.flatradar.app.ui.admin.AdminUsersScreen

/**
 * Root navigation scaffold.
 * Phone: NavigationBar 4-tab / Tablet: NavigationRail 6-tab.
 */
@Composable
fun AppNavigation(
    windowSizeClass: WindowWidthSizeClass,
    isAuthenticated: Boolean = false,
    isAdmin: Boolean = false,
    isUser: Boolean = false,
    userName: String? = null,
    preferences: AppPreferences = AppPreferences(),
    onLoginSuccess: () -> Unit = {},
    onLogout: () -> Unit = {},
    onDeleteAccount: () -> Unit = {}
) {
    val navController = rememberNavController()
    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route

    // Show login if not authenticated
    if (!isAuthenticated) {
        LoginScreen(onLoginSuccess = onLoginSuccess)
        return
    }

    val useWideNav = windowSizeClass == WindowWidthSizeClass.Expanded
    val tabs = if (useWideNav) TopLevelDestination.tabletTabs else TopLevelDestination.phoneTabs
    val visibleTabs = if (isAdmin) tabs else tabs.filter { !it.adminOnly }
    val notificationsViewModel: NotificationsViewModel? = if (isUser || isAdmin) hiltViewModel() else null
    val notificationsState by notificationsViewModel?.uiState?.collectAsStateWithLifecycle()
        ?: remember { mutableStateOf(null) }
    val unreadNotifications = notificationsState?.unreadCount ?: 0

    Scaffold(
        contentWindowInsets = WindowInsets(0.dp),
        bottomBar = {
            if (!useWideNav) {
                FlatRadarNavigationBar(
                    tabs = visibleTabs,
                    currentRoute = currentRoute,
                    unreadNotifications = unreadNotifications,
                    onTabSelected = { dest ->
                        navController.navigateTopLevel(dest, currentRoute)
                    }
                )
            }
        }
    ) { innerPadding ->
        if (useWideNav) {
            Row(modifier = Modifier.padding(innerPadding)) {
                FlatRadarNavigationRail(
                    tabs = visibleTabs,
                    currentRoute = currentRoute,
                    unreadNotifications = unreadNotifications,
                    onTabSelected = { dest ->
                        navController.navigateTopLevel(dest, currentRoute)
                    }
                )
                NavHost(
                    navController = navController,
                    startDestination = TopLevelDestination.DASHBOARD.route,
                    modifier = Modifier.weight(1f),
                    enterTransition = { EnterTransition.None },
                    exitTransition = { ExitTransition.None }
                ) {
                    flatRadarNavGraph(
                        navController = navController,
                        isAdmin = isAdmin,
                        isUser = isUser,
                        userName = userName,
                        preferences = preferences,
                        notificationsViewModel = notificationsViewModel,
                        onLogout = onLogout,
                        onDeleteAccount = onDeleteAccount
                    )
                }
            }
        } else {
            NavHost(
                navController = navController,
                startDestination = TopLevelDestination.DASHBOARD.route,
                modifier = Modifier.padding(innerPadding),
                enterTransition = { EnterTransition.None },
                exitTransition = { ExitTransition.None }
            ) {
                flatRadarNavGraph(
                    navController = navController,
                    isAdmin = isAdmin,
                    isUser = isUser,
                    userName = userName,
                    preferences = preferences,
                    notificationsViewModel = notificationsViewModel,
                    onLogout = onLogout,
                    onDeleteAccount = onDeleteAccount
                )
            }
        }
    }
}

private fun NavHostController.navigateTopLevel(
    dest: TopLevelDestination,
    currentRoute: String?
) {
    val fromSecondaryRoute = currentRoute?.isSecondaryRoute() == true
    navigate(dest.route) {
        popUpTo(TopLevelDestination.DASHBOARD.route) {
            saveState = !fromSecondaryRoute
        }
        launchSingleTop = true
        restoreState = !fromSecondaryRoute
    }
}

private fun String.isSecondaryRoute(): Boolean =
    startsWith("listing") ||
        this in setOf(
            "filter_edit",
            "feedback",
            "admin_users",
            "admin_monitor",
            "legal_terms",
            "legal_privacy"
        )

private fun androidx.navigation.NavGraphBuilder.flatRadarNavGraph(
    navController: NavHostController,
    isAdmin: Boolean,
    isUser: Boolean,
    userName: String?,
    preferences: AppPreferences,
    notificationsViewModel: NotificationsViewModel?,
    onLogout: () -> Unit,
    onDeleteAccount: () -> Unit
) {
    composable(TopLevelDestination.DASHBOARD.route) {
        DashboardScreen(
            isUser = isUser,
            userName = userName,
            onOpenListing = { id -> navController.navigate("listing/$id") },
            onNavigateToBrowse = {
                navController.navigate(TopLevelDestination.LISTINGS.route) {
                    popUpTo(TopLevelDestination.DASHBOARD.route) { saveState = true }
                    launchSingleTop = true
                    restoreState = true
                }
            },
            onLogout = onLogout
        )
    }
    composable(TopLevelDestination.BROWSE.route) {
        BrowseScreen(onOpenDetail = { id -> navController.navigate("listing/$id") })
    }
    composable(TopLevelDestination.LISTINGS.route) {
        ListingsScreen(onOpenDetail = { id -> navController.navigate("listing/$id") })
    }
    composable(TopLevelDestination.MAP.route) {
        MapScreen(onOpenDetail = { id -> navController.navigate("listing/$id") })
    }
    composable(TopLevelDestination.CALENDAR.route) {
        CalendarScreen(onOpenDetail = { id -> navController.navigate("listing/$id") })
    }
    composable(TopLevelDestination.NOTIFICATIONS.route) {
        if (notificationsViewModel != null) {
            NotificationsScreen(
                onOpenDetail = { id -> navController.navigate("listing/$id") },
                viewModel = notificationsViewModel
            )
        } else {
            NotificationsScreen(onOpenDetail = { id -> navController.navigate("listing/$id") })
        }
    }
    composable(TopLevelDestination.SETTINGS.route) {
        SettingsScreen(
            isAdmin = isAdmin,
            isUser = isUser,
            userName = userName,
            preferences = preferences,
            onNavigateToFilterEdit = { navController.navigate("filter_edit") },
            onNavigateToFeedback = { navController.navigate("feedback") },
            onNavigateToAdminUsers = { navController.navigate("admin_users") },
            onNavigateToAdminMonitor = { navController.navigate("admin_monitor") },
            onNavigateToTerms = { navController.navigate("legal_terms") },
            onNavigateToPrivacy = { navController.navigate("legal_privacy") },
            onLogout = onLogout,
            onDeleteAccount = onDeleteAccount
        )
    }
    composable("filter_edit") {
        FilterEditScreen(onBack = { navController.popBackStack() })
    }
    composable("feedback") {
        FeedbackScreen(
            userName = userName.orEmpty(),
            onBack = { navController.popBackStack() }
        )
    }
    composable("admin_users") {
        AdminUsersScreen(onBack = { navController.popBackStack() })
    }
    composable("admin_monitor") {
        AdminMonitorScreen(onBack = { navController.popBackStack() })
    }
    composable("legal_terms") {
        LegalScreen(
            title = "Terms of Use",
            content = LegalText.TERMS,
            onBack = { navController.popBackStack() }
        )
    }
    composable("legal_privacy") {
        LegalScreen(
            title = "Privacy Policy",
            content = LegalText.PRIVACY,
            onBack = { navController.popBackStack() }
        )
    }
    composable("listing/{id}") { backStackEntry ->
        val id = backStackEntry.arguments?.getString("id") ?: ""
        ListingDetailScreen(
            listingId = id,
            onBack = { navController.popBackStack() }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun BrowseScreen(
    onOpenDetail: (String) -> Unit
) {
    var mode by rememberSaveable { mutableStateOf(BrowseMode.LIST) }

    Column(
        Modifier
            .fillMaxSize()
            .statusBarsPadding()
    ) {
        PrimaryTabRow(selectedTabIndex = mode.ordinal) {
            BrowseMode.entries.forEach { browseMode ->
                Tab(
                    selected = mode == browseMode,
                    onClick = { mode = browseMode },
                    text = {
                        Text(
                            when (browseMode) {
                                BrowseMode.LIST -> "List"
                                BrowseMode.MAP -> "Map"
                                BrowseMode.CALENDAR -> "Calendar"
                            }
                        )
                    },
                    icon = {
                        Icon(
                            when (browseMode) {
                                BrowseMode.LIST -> Icons.AutoMirrored.Filled.ViewList
                                BrowseMode.MAP -> Icons.Filled.Map
                                BrowseMode.CALENDAR -> Icons.Filled.CalendarMonth
                            },
                            contentDescription = null
                        )
                    }
                )
            }
        }
        Box(Modifier.weight(1f)) {
            when (mode) {
                BrowseMode.LIST -> ListingsScreen(onOpenDetail = onOpenDetail)
                BrowseMode.MAP -> MapScreen(onOpenDetail = onOpenDetail)
                BrowseMode.CALENDAR -> CalendarScreen(onOpenDetail = onOpenDetail)
            }
        }
    }
}

// ── Navigation bars ────────────────────────────────────────────────

@Composable
private fun FlatRadarNavigationBar(
    tabs: List<TopLevelDestination>,
    currentRoute: String?,
    unreadNotifications: Int,
    onTabSelected: (TopLevelDestination) -> Unit
) {
    NavigationBar(
        modifier = Modifier.height(80.dp),
        containerColor = MaterialTheme.colorScheme.surfaceContainer,
        tonalElevation = 3.dp
    ) {
        tabs.forEach { dest ->
            NavigationBarItem(
                selected = currentRoute == dest.route,
                onClick = { onTabSelected(dest) },
                icon = { DestinationIcon(dest = dest, unreadNotifications = unreadNotifications) },
                label = { Text(dest.iconDescription) }
            )
        }
    }
}

@Composable
private fun FlatRadarNavigationRail(
    tabs: List<TopLevelDestination>,
    currentRoute: String?,
    unreadNotifications: Int,
    onTabSelected: (TopLevelDestination) -> Unit
) {
    NavigationRail(containerColor = MaterialTheme.colorScheme.surfaceContainer) {
        tabs.forEach { dest ->
            NavigationRailItem(
                selected = currentRoute == dest.route,
                onClick = { onTabSelected(dest) },
                icon = { DestinationIcon(dest = dest, unreadNotifications = unreadNotifications) },
                label = { Text(dest.iconDescription) }
            )
        }
    }
}

@Composable
private fun DestinationIcon(dest: TopLevelDestination, unreadNotifications: Int) {
    val showBadge = dest == TopLevelDestination.NOTIFICATIONS && unreadNotifications > 0
    if (showBadge) {
        BadgedBox(
            badge = {
                Badge {
                    Text(if (unreadNotifications > 99) "99+" else unreadNotifications.toString())
                }
            }
        ) {
            Icon(dest.icon(), contentDescription = dest.iconDescription)
        }
    } else {
        Icon(dest.icon(), contentDescription = dest.iconDescription)
    }
}

private fun TopLevelDestination.icon(): ImageVector = when (this) {
    TopLevelDestination.DASHBOARD -> Icons.Filled.Home
    TopLevelDestination.BROWSE -> Icons.Filled.Search
    TopLevelDestination.LISTINGS -> Icons.AutoMirrored.Filled.ViewList
    TopLevelDestination.MAP -> Icons.Filled.Map
    TopLevelDestination.CALENDAR -> Icons.Filled.CalendarMonth
    TopLevelDestination.NOTIFICATIONS -> Icons.Filled.Notifications
    TopLevelDestination.SETTINGS -> Icons.Filled.Settings
}
