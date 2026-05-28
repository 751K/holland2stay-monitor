package com.flatradar.app.ui.auth

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Fingerprint
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flatradar.app.R
import com.flatradar.app.ui.settings.LegalText


private val BackMountainPoints = listOf(
    Pair(0f, 0.70f), Pair(0.07f, 0.52f), Pair(0.13f, 0.68f), Pair(0.20f, 0.45f), Pair(0.26f, 0.28f),
    Pair(0.34f, 0.55f), Pair(0.42f, 0.35f), Pair(0.50f, 0.58f), Pair(0.56f, 0.45f), Pair(0.63f, 0.70f),
    Pair(0.70f, 0.30f), Pair(0.77f, 0.62f), Pair(0.84f, 0.48f), Pair(0.91f, 0.70f), Pair(1.0f, 0.48f),
    Pair(1.0f, 1.0f), Pair(0f, 1.0f)
)

private val FrontMountainPoints = listOf(
    Pair(0f, 0.72f), Pair(0.05f, 0.50f), Pair(0.12f, 0.72f), Pair(0.18f, 0.40f), Pair(0.25f, 0.24f),
    Pair(0.34f, 0.62f), Pair(0.41f, 0.34f), Pair(0.49f, 0.70f), Pair(0.55f, 0.55f), Pair(0.63f, 0.80f),
    Pair(0.70f, 0.42f), Pair(0.77f, 0.72f), Pair(0.84f, 0.45f), Pair(0.91f, 0.72f), Pair(1.0f, 0.58f),
    Pair(1.0f, 1.0f), Pair(0f, 1.0f)
)

/**
 * Login screen — matches iOS LoginView + LoginModePicker.
 *
 * Three modes: Tenant (user), Guest, Staff (admin).
 * Expanding card for selected role with credential inputs.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LoginScreen(
    onLoginSuccess: () -> Unit = {},
    viewModel: AuthViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val context = LocalContext.current

    // Redirect on successful login
    LaunchedEffect(state.isAuthenticated) {
        if (state.isAuthenticated) onLoginSuccess()
    }

    var selectedRole by remember { mutableStateOf("user") }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var showRegisterSheet by remember { mutableStateOf(false) }
    var showTermsSheet by remember { mutableStateOf(false) }
    var showPrivacySheet by remember { mutableStateOf(false) }
    var saveForBiometric by remember { mutableStateOf(false) }

    Scaffold(
        contentWindowInsets = WindowInsets(0.dp)
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            HeroHeaderSection(selectedRole)

            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 24.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Spacer(modifier = Modifier.height(24.dp))

            // Role selector cards
            Row(
                modifier = Modifier.fillMaxWidth().height(IntrinsicSize.Max),
                horizontalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                RoleCard(
                    title = "Tenant",
                    subtitle = "User account",
                    icon = Icons.Filled.Person,
                    selected = selectedRole == "user",
                    onClick = { selectedRole = "user" },
                    modifier = Modifier.weight(1f).fillMaxHeight()
                )
                RoleCard(
                    title = "Guest",
                    subtitle = "Browse only",
                    icon = Icons.Filled.Visibility,
                    selected = selectedRole == "guest",
                    onClick = { selectedRole = "guest" },
                    modifier = Modifier.weight(1f).fillMaxHeight()
                )
            }

            Spacer(modifier = Modifier.height(24.dp))

            // Expandable credential area
            AnimatedVisibility(
                visible = selectedRole == "user" || selectedRole == "admin",
                enter = expandVertically() + fadeIn(),
                exit = shrinkVertically() + fadeOut()
            ) {
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    AnimatedVisibility(
                        visible = selectedRole == "user",
                        enter = expandVertically() + fadeIn(),
                        exit = shrinkVertically() + fadeOut()
                    ) {
                        Column {
                            OutlinedTextField(
                                value = username,
                                onValueChange = { username = it },
                                label = { Text("Username") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth(),
                                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next)
                            )
                            Spacer(modifier = Modifier.height(12.dp))
                        }
                    }

                    OutlinedTextField(
                        value = password,
                        onValueChange = { password = it },
                        label = { Text("Password") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                        visualTransformation = if (showPassword) VisualTransformation.None
                            else PasswordVisualTransformation(),
                        trailingIcon = {
                            IconButton(onClick = { showPassword = !showPassword }) {
                                Icon(
                                    if (showPassword) Icons.Filled.VisibilityOff
                                    else Icons.Filled.Visibility,
                                    contentDescription = "Toggle password"
                                )
                            }
                        },
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Password,
                            imeAction = ImeAction.Done
                        ),
                        keyboardActions = KeyboardActions(
                            onDone = {
                                when (selectedRole) {
                                    "admin" -> viewModel.loginAsAdmin(password)
                                    "user" -> viewModel.loginAsUser(username, password, saveForBiometric)
                                }
                            }
                        )
                    )

                    AnimatedVisibility(
                        visible = selectedRole == "user" && state.canUseBiometric && !state.hasBiometricCredential,
                        enter = expandVertically() + fadeIn(),
                        exit = shrinkVertically() + fadeOut()
                    ) {
                        Column {
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Checkbox(
                                    checked = saveForBiometric,
                                    onCheckedChange = { saveForBiometric = it },
                                    enabled = !state.isLoading
                                )
                                Text(
                                    "Enable ${state.biometryName} sign-in on this device",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(20.dp))

                    // Sign in button
                    Button(
                        onClick = {
                            when (selectedRole) {
                                "admin" -> viewModel.loginAsAdmin(password)
                                "user" -> viewModel.loginAsUser(username, password, saveForBiometric)
                            }
                        },
                        modifier = Modifier.fillMaxWidth().height(50.dp),
                        enabled = !state.isLoading &&
                            (selectedRole == "admin" || username.isNotBlank()) &&
                            password.isNotBlank()
                    ) {
                        if (state.isLoading) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(20.dp),
                                color = MaterialTheme.colorScheme.onPrimary
                            )
                        } else {
                            Text("Sign in")
                        }
                    }

                    // Biometric button (placed under normal sign in button, visible only when credentials exist and role is user)
                    AnimatedVisibility(
                        visible = selectedRole == "user" && state.canUseBiometric && state.hasBiometricCredential,
                        enter = expandVertically() + fadeIn(),
                        exit = shrinkVertically() + fadeOut()
                    ) {
                        Column {
                            Spacer(modifier = Modifier.height(12.dp))
                            OutlinedButton(
                                onClick = {
                                    context.findActivity()?.let(viewModel::loginWithBiometrics)
                                },
                                enabled = !state.isLoading && !state.isBiometricAuthenticating,
                                modifier = Modifier.fillMaxWidth().height(50.dp)
                            ) {
                                if (state.isBiometricAuthenticating) {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(18.dp),
                                        color = MaterialTheme.colorScheme.primary
                                    )
                                } else {
                                    Icon(Icons.Filled.Fingerprint, contentDescription = null)
                                    Spacer(Modifier.width(8.dp))
                                    Text("Sign in with ${state.biometryName}")
                                }
                            }
                        }
                    }

                    // Register link (tenant only)
                    AnimatedVisibility(
                        visible = selectedRole == "user",
                        enter = expandVertically() + fadeIn(),
                        exit = shrinkVertically() + fadeOut()
                    ) {
                        Column {
                            Spacer(modifier = Modifier.height(12.dp))
                            TextButton(
                                onClick = { showRegisterSheet = true },
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text("Register a new account")
                            }
                        }
                    }
                }
            }

            // Guest entry button
            if (selectedRole == "guest") {
                Button(
                    onClick = { viewModel.enterAsGuest() },
                    modifier = Modifier.fillMaxWidth().height(50.dp)
                ) {
                    Text("Enter as Guest")
                }
            }

            // Error message
            state.errorMessage?.let { error ->
                Spacer(modifier = Modifier.height(16.dp))
                Text(
                    text = error,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    textAlign = TextAlign.Center
                )
            }

                Spacer(modifier = Modifier.height(24.dp))
                HorizontalDivider(
                    modifier = Modifier.padding(horizontal = 16.dp),
                    color = MaterialTheme.colorScheme.outlineVariant
                )
                Spacer(modifier = Modifier.height(16.dp))
                Text(
                    text = "Unofficial third-party client. Not affiliated with, endorsed by, or sponsored by monitored housing platforms. All listing data belongs to its respective owners.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(horizontal = 16.dp)
                )
                Spacer(modifier = Modifier.height(12.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.Center
                ) {
                    TextButton(
                        onClick = { showTermsSheet = true },
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            text = "Terms of Use",
                            style = MaterialTheme.typography.labelLarge,
                            fontWeight = FontWeight.Medium
                        )
                    }
                    Text(
                        text = "·",
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(horizontal = 4.dp),
                        style = MaterialTheme.typography.bodyMedium
                    )
                    TextButton(
                        onClick = { showPrivacySheet = true },
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            text = "Privacy Policy",
                            style = MaterialTheme.typography.labelLarge,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = "flatradar.app",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                    modifier = Modifier.padding(bottom = 24.dp)
                )
            }
        }
    }

    // Register sheet
    if (showRegisterSheet) {
        val regSheetState = rememberModalBottomSheetState()
        ModalBottomSheet(
            onDismissRequest = { showRegisterSheet = false },
            sheetState = regSheetState
        ) {
            RegisterSheet(
                isLoading = state.isLoading,
                canUseBiometric = state.canUseBiometric && !state.hasBiometricCredential,
                biometryName = state.biometryName,
                onRegister = { user, pass, save ->
                    viewModel.register(user, pass, save)
                    showRegisterSheet = false
                }
            )
        }
    }

    // Terms sheet
    if (showTermsSheet) {
        val sheetState = rememberModalBottomSheetState()
        ModalBottomSheet(
            onDismissRequest = { showTermsSheet = false },
            sheetState = sheetState
        ) {
            LegalSheetContent(
                title = "Terms of Use",
                content = LegalText.TERMS,
                onDismiss = { showTermsSheet = false }
            )
        }
    }

    // Privacy sheet
    if (showPrivacySheet) {
        val sheetState = rememberModalBottomSheetState()
        ModalBottomSheet(
            onDismissRequest = { showPrivacySheet = false },
            sheetState = sheetState
        ) {
            LegalSheetContent(
                title = "Privacy Policy",
                content = LegalText.PRIVACY,
                onDismiss = { showPrivacySheet = false }
            )
        }
    }
}

@Composable
private fun RoleCard(
    title: String,
    subtitle: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    Card(
        onClick = onClick,
        modifier = modifier,
        shape = MaterialTheme.shapes.medium,
        colors = CardDefaults.cardColors(
            containerColor = if (selected)
                MaterialTheme.colorScheme.secondaryContainer
            else
                MaterialTheme.colorScheme.surfaceContainer
        ),
        border = if (selected)
            androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.primary)
        else null
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight()
                .padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Icon(
                icon,
                contentDescription = title,
                tint = if (selected) MaterialTheme.colorScheme.onSecondaryContainer
                    else MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                title,
                style = MaterialTheme.typography.titleMedium,
                color = if (selected) MaterialTheme.colorScheme.onSecondaryContainer
                    else MaterialTheme.colorScheme.onSurface,
                textAlign = TextAlign.Center
            )
            Text(
                subtitle,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RegisterSheet(
    isLoading: Boolean,
    canUseBiometric: Boolean,
    biometryName: String,
    onRegister: (String, String, Boolean) -> Unit
) {
    var regUsername by remember { mutableStateOf("") }
    var regPassword by remember { mutableStateOf("") }
    var saveForBiometric by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier.padding(horizontal = 24.dp, vertical = 16.dp)
    ) {
        Text(
            "Create account",
            style = MaterialTheme.typography.headlineMedium
        )
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedTextField(
            value = regUsername,
            onValueChange = { regUsername = it },
            label = { Text("Username (min 2 chars)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(12.dp))
        OutlinedTextField(
            value = regPassword,
            onValueChange = { regPassword = it },
            label = { Text("Password (min 4 chars)") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth()
        )
        if (canUseBiometric) {
            Spacer(modifier = Modifier.height(8.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Checkbox(
                    checked = saveForBiometric,
                    onCheckedChange = { saveForBiometric = it },
                    enabled = !isLoading
                )
                Text(
                    "Enable $biometryName sign-in on this device",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
        Spacer(modifier = Modifier.height(20.dp))
        Button(
            onClick = { onRegister(regUsername, regPassword, saveForBiometric) },
            modifier = Modifier.fillMaxWidth().height(50.dp),
            enabled = !isLoading && regUsername.length >= 2 && regPassword.length >= 4
        ) {
            Text("Register & Sign in")
        }
        Spacer(modifier = Modifier.height(32.dp))
    }
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}

@Composable
private fun HeroHeaderSection(selectedRole: String) {
    val isDark = isSystemInDarkTheme()
    val brandBlue = Color(0xFF0A84FF)

    val bgGradient = remember(isDark) {
        if (isDark) {
            Brush.verticalGradient(listOf(Color(0xFF141E38), Color(0xFF0F1A2E)))
        } else {
            Brush.verticalGradient(listOf(Color(0xFFE6F2FF), Color(0xFFD1E6FC)))
        }
    }

    val mountainBack = if (isDark) Color(0xFF101C35) else Color(0xFF66809E)
    val mountainFront = if (isDark) Color(0xFF060D1C) else Color(0xFF507096)

    val headlineColor = if (isDark) Color(0xFFEBEDF2) else Color(0xFF0D111A)
    val descriptionColor = if (isDark) Color(0xFF9CA4B8) else Color(0xFF6B7080)

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(320.dp)
            .background(bgGradient)
    ) {
        // Back mountains
        MountainPath(
            points = BackMountainPoints,
            color = mountainBack,
            modifier = Modifier
                .fillMaxWidth()
                .height(115.dp)
                .align(Alignment.BottomCenter)
        )

        // Front mountains
        MountainPath(
            points = FrontMountainPoints,
            color = mountainFront,
            modifier = Modifier
                .fillMaxWidth()
                .height(95.dp)
                .align(Alignment.BottomCenter)
        )

        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 22.dp)
                .padding(top = 48.dp),
            verticalArrangement = Arrangement.Top
        ) {
            // App Identity Row
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                Box(
                    modifier = Modifier
                        .size(48.dp)
                        .clip(CircleShape)
                        .background(Color.White)
                        .padding(8.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Image(
                        painter = painterResource(id = R.drawable.app_logo),
                        contentDescription = "FlatRadar App Logo",
                        modifier = Modifier.fillMaxSize()
                    )
                }

                Spacer(modifier = Modifier.width(12.dp))

                Column {
                    Text(
                        text = "FlatRadar",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Black,
                        color = brandBlue
                    )
                    Text(
                        text = "UNOFFICIAL · v1.7.8",
                        style = MaterialTheme.typography.labelSmall,
                        color = descriptionColor,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                    )
                }
            }

            // Dynamic Headline
            val headline = when (selectedRole) {
                "guest" -> "Browse listings\nread-only."
                "user", "admin" -> "Sign in to your\naccount."
                else -> "Searching for a new\nhome in the Netherlands?"
            }

            Text(
                text = headline,
                style = MaterialTheme.typography.headlineLarge,
                fontWeight = FontWeight.Black,
                color = headlineColor,
                lineHeight = 34.sp,
                modifier = Modifier.padding(top = 24.dp)
            )

            Spacer(modifier = Modifier.height(10.dp))

            Text(
                text = "A real-time monitor for multiple rental platforms.",
                style = MaterialTheme.typography.bodyLarge,
                color = descriptionColor
            )
        }
    }
}

@Composable
private fun MountainPath(
    points: List<Pair<Float, Float>>,
    color: Color,
    modifier: Modifier = Modifier
) {
    Spacer(
        modifier = modifier.drawWithCache {
            val path = Path().apply {
                if (points.isNotEmpty()) {
                    val first = points.first()
                    moveTo(first.first * size.width, first.second * size.height)
                    for (i in 1 until points.size) {
                        val pt = points[i]
                        lineTo(pt.first * size.width, pt.second * size.height)
                    }
                    close()
                }
            }
            onDrawBehind {
                drawPath(path = path, color = color)
            }
        }
    )
}

@Composable
private fun LegalSheetContent(
    title: String,
    content: String,
    onDismiss: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .navigationBarsPadding()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 24.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold
            )
            TextButton(onClick = onDismiss) {
                Text("Done")
            }
        }
        HorizontalDivider()
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = false)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp, vertical = 16.dp)
        ) {
            Text(
                text = content,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                lineHeight = 20.sp
            )
            Spacer(modifier = Modifier.height(32.dp))
        }
    }
}

