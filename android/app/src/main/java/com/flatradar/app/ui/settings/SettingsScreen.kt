package com.flatradar.app.ui.settings

import android.content.Intent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.DarkMode
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Feedback
import androidx.compose.material.icons.filled.Group
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Password
import androidx.compose.material.icons.filled.Policy
import androidx.compose.material.icons.filled.Storage
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.ui.text.font.FontWeight
import com.flatradar.app.BuildConfig
import com.flatradar.app.domain.model.ListingFilter
import com.flatradar.app.data.local.AppColorScheme
import com.flatradar.app.data.local.AppPreferences

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    isAdmin: Boolean = false,
    isUser: Boolean = false,
    userName: String? = null,
    preferences: AppPreferences = AppPreferences(),
    onNavigateToFeedback: () -> Unit = {},
    onNavigateToFilterEdit: () -> Unit = {},
    onNavigateToAdminUsers: () -> Unit = {},
    onNavigateToAdminMonitor: () -> Unit = {},
    onNavigateToTerms: () -> Unit = {},
    onNavigateToPrivacy: () -> Unit = {},
    onLogout: () -> Unit = {},
    onDeleteAccount: () -> Unit = {},
    viewModel: SettingsViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val snackbarHostState = remember { SnackbarHostState() }
    val context = LocalContext.current
    var showDeleteDialog by remember { mutableStateOf(false) }
    var showChangePassword by remember { mutableStateOf(false) }
    var showLogoutDialog by remember { mutableStateOf(false) }

    LaunchedEffect(state.message) {
        state.message?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearMessage()
        }
    }
    LaunchedEffect(state.passwordChanged) {
        if (state.passwordChanged) {
            showChangePassword = false
            viewModel.clearPasswordChanged()
        }
    }
    LaunchedEffect(state.exportJson) {
        val json = state.exportJson ?: return@LaunchedEffect
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "application/json"
            putExtra(Intent.EXTRA_SUBJECT, "FlatRadar data export")
            putExtra(Intent.EXTRA_TEXT, json)
        }
        context.startActivity(Intent.createChooser(intent, "Export My Data"))
        viewModel.clearExport()
    }

    LaunchedEffect(isUser) {
        if (isUser) {
            viewModel.loadFilter()
        }
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Settings") }) },
        snackbarHost = { SnackbarHost(snackbarHostState) }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(bottom = 24.dp)
        ) {
            userName?.takeIf { it.isNotBlank() }?.let {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = MaterialTheme.shapes.medium,
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)
                ) {
                    Row(
                        modifier = Modifier.padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Surface(
                            shape = CircleShape,
                            color = MaterialTheme.colorScheme.tertiaryContainer,
                            contentColor = MaterialTheme.colorScheme.onTertiaryContainer
                        ) {
                            Box(Modifier.size(40.dp), contentAlignment = Alignment.Center) {
                                Icon(Icons.Filled.AccountCircle, null)
                            }
                        }
                        Spacer(Modifier.width(16.dp))
                        Column {
                            Text(it, style = MaterialTheme.typography.titleMedium)
                            Text(
                                if (isAdmin) "Administrator" else "Tenant account",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                }
            }


            SettingsSectionTitle("Appearance")
            Row(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(Icons.Filled.DarkMode, null, Modifier.size(22.dp))
                Spacer(Modifier.width(16.dp))
                AppColorScheme.entries.forEach { scheme ->
                    FilterChip(
                        selected = preferences.colorScheme == scheme,
                        onClick = { viewModel.saveColorScheme(scheme) },
                        label = {
                            Text(
                                when (scheme) {
                                    AppColorScheme.SYSTEM -> "System"
                                    AppColorScheme.LIGHT -> "Light"
                                    AppColorScheme.DARK -> "Dark"
                                }
                            )
                        },
                        modifier = Modifier.padding(end = 8.dp)
                    )
                }
            }

            if (isUser) {
                HorizontalDivider()
                SettingsItem(
                    icon = Icons.Filled.Tune,
                    label = "Push Notification Filter",
                    subtitle = formatFilterText(state.currentFilter),
                    onClick = onNavigateToFilterEdit
                )
                if (state.canUseBiometric && state.hasBiometricCredential) {
                    SettingsItem(
                        Icons.Filled.Lock,
                        "Remove ${state.biometryName} Sign-In",
                        onClick = viewModel::deleteBiometricSignIn
                    )
                }
                SettingsItem(Icons.Filled.Password, "Change Password", onClick = { showChangePassword = true })
                SettingsItem(
                    Icons.Filled.Storage,
                    if (state.isExporting) "Exporting My Data..." else "Export My Data",
                    onClick = { if (!state.isExporting) viewModel.exportMyData() }
                )
            }
            if (!isAdmin) {
                HorizontalDivider()
                SettingsItem(Icons.Filled.Policy, "Terms of Use", onClick = onNavigateToTerms)
                SettingsItem(Icons.Filled.Policy, "Privacy Policy", onClick = onNavigateToPrivacy)
            }

            SettingsItem(Icons.Filled.Feedback, "Send Feedback", onClick = onNavigateToFeedback)
            if (isAdmin) {
                HorizontalDivider()
                SettingsItem(Icons.Filled.Group, "Admin Users", onClick = onNavigateToAdminUsers)
                SettingsItem(Icons.Filled.Memory, "Monitor Control", onClick = onNavigateToAdminMonitor)
            }

            HorizontalDivider()
            SettingsItem(Icons.Filled.Info, "About FlatRadar v${BuildConfig.VERSION_NAME}", onClick = {})
            SettingsItem(Icons.AutoMirrored.Filled.Logout, "Log Out", onClick = { showLogoutDialog = true })
            if (isUser) {
                SettingsItem(
                    Icons.Filled.Delete,
                    "Delete Account",
                    onClick = { showDeleteDialog = true },
                    tint = MaterialTheme.colorScheme.error
                )
            }
        }
    }

    if (showDeleteDialog) {
        AlertDialog(
            onDismissRequest = { showDeleteDialog = false },
            title = { Text("Delete Account") },
            text = { Text("This will permanently delete your account and all data. This action cannot be undone.") },
            confirmButton = {
                TextButton(onClick = {
                    showDeleteDialog = false
                    onDeleteAccount()
                }) { Text("Delete", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { showDeleteDialog = false }) { Text("Cancel") }
            }
        )
    }

    if (showLogoutDialog) {
        AlertDialog(
            onDismissRequest = { showLogoutDialog = false },
            title = { Text("Log Out") },
            text = { Text("Are you sure you want to log out?") },
            confirmButton = {
                TextButton(onClick = {
                    showLogoutDialog = false
                    onLogout()
                }) { Text("Log Out", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { showLogoutDialog = false }) { Text("Cancel") }
            }
        )
    }

    if (showChangePassword) {
        ChangePasswordSheet(
            isSubmitting = state.isChangingPassword,
            onDismiss = { showChangePassword = false },
            onSubmit = viewModel::changePassword
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChangePasswordSheet(
    isSubmitting: Boolean,
    onDismiss: () -> Unit,
    onSubmit: (String, String) -> Unit
) {
    var currentPassword by remember { mutableStateOf("") }
    var newPassword by remember { mutableStateOf("") }
    var confirmPassword by remember { mutableStateOf("") }

    val inlineError = when {
        currentPassword.isBlank() || newPassword.isBlank() || confirmPassword.isBlank() -> "All fields are required."
        newPassword.length < 4 -> "New password must be at least 4 characters."
        newPassword != confirmPassword -> "New passwords do not match."
        currentPassword == newPassword -> "New password must be different."
        else -> null
    }

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 24.dp, vertical = 16.dp)) {
            Text("Change Password", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.height(16.dp))
            OutlinedTextField(
                value = currentPassword,
                onValueChange = { currentPassword = it },
                label = { Text("Current password") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(10.dp))
            OutlinedTextField(
                value = newPassword,
                onValueChange = { newPassword = it },
                label = { Text("New password") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(10.dp))
            OutlinedTextField(
                value = confirmPassword,
                onValueChange = { confirmPassword = it },
                label = { Text("Confirm new password") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            inlineError?.let {
                Spacer(Modifier.height(8.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            Spacer(Modifier.height(18.dp))
            Button(
                onClick = { onSubmit(currentPassword, newPassword) },
                enabled = inlineError == null && !isSubmitting,
                modifier = Modifier.fillMaxWidth()
            ) {
                if (isSubmitting) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        color = MaterialTheme.colorScheme.onPrimary
                    )
                } else {
                    Text("Save Password")
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun SettingsSectionTitle(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp)
    )
}

@Composable
private fun SettingsItem(
    icon: ImageVector,
    label: String,
    subtitle: String? = null,
    onClick: () -> Unit,
    tint: androidx.compose.ui.graphics.Color = MaterialTheme.colorScheme.onSurface
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Surface(
            shape = MaterialTheme.shapes.medium,
            color = if (tint == MaterialTheme.colorScheme.error)
                MaterialTheme.colorScheme.errorContainer
            else
                MaterialTheme.colorScheme.surfaceContainer,
            contentColor = tint
        ) {
            Box(Modifier.size(40.dp), contentAlignment = Alignment.Center) {
                Icon(icon, null, Modifier.size(22.dp), tint = tint)
            }
        }
        Spacer(Modifier.width(16.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = label,
                color = tint,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.SemiBold
            )
            if (subtitle != null) {
                Spacer(Modifier.height(2.dp))
                Text(
                    text = subtitle,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 2
                )
            }
        }
    }
}

private fun formatFilterText(filter: ListingFilter?): String {
    if (filter == null || filter.isEmpty()) return "None (All listings)"
    
    val parts = mutableListOf<String>()
    
    if (filter.cities.isNotEmpty()) {
        parts.add(filter.cities.joinToString(", "))
    }
    
    if (filter.sources.isNotEmpty()) {
        parts.add(filter.sources.joinToString(", "))
    }
    
    if (filter.minRent != null || filter.maxRent != null) {
        val rentStr = when {
            filter.minRent != null && filter.maxRent != null -> "€${filter.minRent}–€${filter.maxRent}"
            filter.minRent != null -> "≥ €${filter.minRent}"
            else -> "≤ €${filter.maxRent}"
        }
        parts.add(rentStr)
    }
    
    if (filter.minArea != null || filter.maxArea != null) {
        val areaStr = when {
            filter.minArea != null && filter.maxArea != null -> "${filter.minArea}–${filter.maxArea} m²"
            filter.minArea != null -> "≥ ${filter.minArea} m²"
            else -> "≤ ${filter.maxArea} m²"
        }
        parts.add(areaStr)
    }
    
    if (filter.types.isNotEmpty()) {
        parts.add(filter.types.joinToString(", "))
    }
    
    if (parts.isEmpty()) return "Active Filter"
    return parts.joinToString(" · ")
}

