package com.flatradar.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.unit.dp
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import com.flatradar.app.data.local.AppPreferences
import com.flatradar.app.data.local.PreferencesManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.navigation.AppNavigation
import com.flatradar.app.ui.auth.AuthViewModel
import com.flatradar.app.ui.components.AppErrorBus
import com.flatradar.app.ui.theme.FlatRadarTheme
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    private val authViewModel: AuthViewModel by viewModels()

    @Inject lateinit var preferencesManager: PreferencesManager
    @Inject lateinit var apiClient: ApiClient
    @Inject lateinit var errorBus: AppErrorBus
    @Inject lateinit var navigationCoordinator: com.flatradar.app.navigation.NavigationCoordinator

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    private fun handleDeepLink(intent: Intent) {
        val uri = intent.data ?: return
        if (uri.scheme != "h2smonitor") return
        val id = uri.lastPathSegment ?: return
        if (id.isNotBlank()) {
            navigationCoordinator.openListing(id)
        }
    }

    @OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        authViewModel.restoreSession()
        handleDeepLink(intent)

        setContent {
            val defaultPreferences = remember { AppPreferences() }
            val preferences by preferencesManager.preferences.collectAsState(defaultPreferences)

            LaunchedEffect(preferences.serverUrl) {
                apiClient.configureBaseUrl(preferences.serverUrl)
            }

            FlatRadarTheme(colorSchemePreference = preferences.colorScheme) {
                val snackbarHostState = remember { SnackbarHostState() }
                val windowSizeClass = calculateWindowSizeClass(this)
                val authState by authViewModel.uiState.collectAsState()

                val sharedPrefs = remember { getSharedPreferences("flatradar_prefs", Context.MODE_PRIVATE) }
                var termsAccepted by remember { mutableStateOf(sharedPrefs.getBoolean("terms_accepted", false)) }

                LaunchedEffect(Unit) {
                    errorBus.messages.collect { snackbarHostState.showSnackbar(it) }
                }

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    val permLauncher = rememberLauncherForActivityResult(
                        androidx.activity.result.contract.ActivityResultContracts.RequestPermission()
                    ) { /* proceed regardless */ }
                    LaunchedEffect(authState.isAuthenticated) {
                        if (authState.isAuthenticated) {
                            permLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
                        }
                    }
                }

                Scaffold(
                    snackbarHost = { SnackbarHost(snackbarHostState) },
                    contentWindowInsets = WindowInsets(0.dp)
                ) { rootPadding ->
                    Box(Modifier.fillMaxSize().padding(rootPadding)) {
                        AppNavigation(
                            windowSizeClass = windowSizeClass.widthSizeClass,
                            isAuthenticated = authState.isAuthenticated,
                            isAdmin = authState.role == "admin",
                            isUser = authState.role == "user",
                            userName = authState.userInfo?.name,
                            preferences = preferences,
                            navigationCoordinator = navigationCoordinator,
                            onLoginSuccess = {},
                            onLogout = { authViewModel.logout() },
                            onDeleteAccount = { authViewModel.deleteAccount() }
                        )

                        if (!termsAccepted) {
                            TermsAgreementSheet(
                                onAgree = {
                                    sharedPrefs.edit().putBoolean("terms_accepted", true).apply()
                                    termsAccepted = true
                                }
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun TermsAgreementSheet(
    onAgree: () -> Unit
) {
    Dialog(
        onDismissRequest = {},
        properties = DialogProperties(
            usePlatformDefaultWidth = false,
            dismissOnBackPress = false,
            dismissOnClickOutside = false
        )
    ) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(Color.Black.copy(alpha = 0.5f)),
            contentAlignment = Alignment.BottomCenter
        ) {
            Surface(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
                color = MaterialTheme.colorScheme.surface,
                tonalElevation = 6.dp
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 24.dp, vertical = 24.dp)
                        .navigationBarsPadding()
                ) {
                    Text(
                        text = "Before You Continue",
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Please read and accept the Terms of Use to use FlatRadar.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )

                    Spacer(modifier = Modifier.height(16.dp))
                    HorizontalDivider()
                    Spacer(modifier = Modifier.height(16.dp))

                    Column(
                        modifier = Modifier
                            .weight(1f, fill = false)
                            .verticalScroll(rememberScrollState())
                    ) {
                        Text(
                            text = "FlatRadar is an independent, unofficial monitoring tool for rental housing listings across multiple platforms. It is not affiliated with, endorsed by, sponsored by, maintained by, or operated by any of the housing platforms it monitors.\n\n" +
                                   "By using FlatRadar, you acknowledge that you have read and agree to the Terms of Use and Privacy Policy.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )

                        Spacer(modifier = Modifier.height(16.dp))
                        HorizontalDivider()
                        Spacer(modifier = Modifier.height(16.dp))

                        Text(
                            text = "By continuing, you agree that:",
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onSurface
                        )

                        Spacer(modifier = Modifier.height(12.dp))

                        BulletPoint("FlatRadar is an unofficial, independent tool. Not affiliated with or endorsed by any housing platform.")
                        BulletPoint("You are responsible for complying with each housing platform's Terms of Service.")
                        BulletPoint("Listing data may be delayed, incomplete, inaccurate, or change without notice.")
                        BulletPoint("Push notifications are best-effort. Always verify listings on the official website.")
                        BulletPoint("FlatRadar is for personal, non-commercial use only.")
                    }

                    Spacer(modifier = Modifier.height(24.dp))

                    Button(
                        onClick = onAgree,
                        modifier = Modifier.fillMaxWidth().height(50.dp)
                    ) {
                        Text("Agree & Continue", fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}

@Composable
private fun BulletPoint(text: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.Start,
        verticalAlignment = Alignment.Top
    ) {
        Text(
            text = "•",
            color = MaterialTheme.colorScheme.primary,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(end = 8.dp)
        )
        Text(
            text = text,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
