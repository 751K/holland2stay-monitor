package com.flatradar.app.data.local

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.flatradar.app.data.remote.ApiClient
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

private val Context.dataStore by preferencesDataStore(name = "flatradar_preferences")

enum class AppColorScheme(val wireValue: String) {
    SYSTEM("system"),
    LIGHT("light"),
    DARK("dark");

    companion object {
        fun from(value: String?): AppColorScheme = entries.firstOrNull { it.wireValue == value } ?: SYSTEM
    }
}

data class AppPreferences(
    val serverUrl: String = ApiClient.DEFAULT_BASE_URL,
    val colorScheme: AppColorScheme = AppColorScheme.SYSTEM
)

@Singleton
class PreferencesManager @Inject constructor(
    @ApplicationContext private val context: Context
) {
    val preferences: Flow<AppPreferences> = context.dataStore.data.map { prefs ->
        AppPreferences(
            serverUrl = prefs[SERVER_URL]?.takeIf { it.isNotBlank() } ?: ApiClient.DEFAULT_BASE_URL,
            colorScheme = AppColorScheme.from(prefs[COLOR_SCHEME])
        )
    }

    suspend fun setServerUrl(url: String) {
        val normalized = normalizeServerUrl(url)
        context.dataStore.edit { prefs ->
            prefs[SERVER_URL] = normalized
        }
    }

    suspend fun setColorScheme(scheme: AppColorScheme) {
        context.dataStore.edit { prefs ->
            prefs[COLOR_SCHEME] = scheme.wireValue
        }
    }

    companion object {
        private val SERVER_URL = stringPreferencesKey("server_url")
        private val COLOR_SCHEME = stringPreferencesKey("color_scheme")

        fun normalizeServerUrl(raw: String): String {
            val trimmed = raw.trim().trimEnd('/')
            if (trimmed.isBlank()) return ApiClient.DEFAULT_BASE_URL
            val withScheme = if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
                trimmed
            } else {
                "https://$trimmed"
            }
            return withScheme.trimEnd('/')
        }

        fun isValidServerUrl(raw: String): Boolean {
            val normalized = normalizeServerUrl(raw)
            return normalized.startsWith("http://") || normalized.startsWith("https://")
        }
    }
}
