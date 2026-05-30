package com.flatradar.app.data.local

import android.app.Activity
import android.content.Context
import android.content.SharedPreferences
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.CancellationSignal
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.concurrent.Executor
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Local biometric sign-in helper.
 *
 * The actual biometric template stays in Android system services. FlatRadar only stores
 * the opted-in user credentials in encrypted local storage and reads them after a
 * successful BiometricPrompt authentication.
 */
@Singleton
class BiometricAuth @Inject constructor(
    @param:ApplicationContext private val context: Context
) {
    data class StoredCredential(
        val username: String,
        val password: String,
        val role: String
    )

    private val prefs: SharedPreferences by lazy {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()

        EncryptedSharedPreferences.create(
            context,
            PREFS_FILE,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    fun isAvailable(): Boolean {
        val manager = context.getSystemService(BiometricManager::class.java) ?: return false
        return manager.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) ==
            BiometricManager.BIOMETRIC_SUCCESS
    }

    fun biometryName(): String = "Biometrics"

    fun hasStoredUserCredentials(): Boolean = prefs.getString(KEY_ROLE, null) == "user"

    fun saveCredentials(username: String, password: String, role: String) {
        if (role != "user") return
        prefs.edit()
            .putString(KEY_USERNAME, username)
            .putString(KEY_PASSWORD, password)
            .putString(KEY_ROLE, role)
            .apply()
    }

    fun deleteCredentials() {
        prefs.edit()
            .remove(KEY_USERNAME)
            .remove(KEY_PASSWORD)
            .remove(KEY_ROLE)
            .apply()
    }

    fun authenticateAndLoad(
        activity: Activity,
        onSuccess: (StoredCredential) -> Unit,
        onError: (String) -> Unit
    ) {
        if (!isAvailable()) {
            onError("Biometric sign-in is not available on this device.")
            return
        }
        if (!hasStoredUserCredentials()) {
            onError("No biometric sign-in is saved for this account.")
            return
        }

        val executor: Executor = activity.mainExecutor
        val prompt = BiometricPrompt.Builder(activity)
            .setTitle("Unlock FlatRadar")
            .setSubtitle("Sign in with biometrics")
            .setNegativeButton("Use password", executor) { _, _ ->
                onError("Biometric sign-in cancelled.")
            }
            .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
            .build()

        prompt.authenticate(
            CancellationSignal(),
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                    val credential = loadCredentials()
                    if (credential != null) {
                        onSuccess(credential)
                    } else {
                        onError("Unable to read saved biometric sign-in.")
                    }
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                    onError(errString?.toString() ?: "Biometric sign-in failed.")
                }

                override fun onAuthenticationFailed() {
                    onError("Biometric authentication did not match.")
                }
            }
        )
    }

    private fun loadCredentials(): StoredCredential? {
        val username = prefs.getString(KEY_USERNAME, null)?.takeIf { it.isNotBlank() }
        val password = prefs.getString(KEY_PASSWORD, null)?.takeIf { it.isNotBlank() }
        val role = prefs.getString(KEY_ROLE, null)?.takeIf { it == "user" }
        return if (username != null && password != null && role != null) {
            StoredCredential(username, password, role)
        } else {
            null
        }
    }

    companion object {
        private const val PREFS_FILE = "flatradar_biometric_prefs"
        private const val KEY_USERNAME = "biometric_username"
        private const val KEY_PASSWORD = "biometric_password"
        private const val KEY_ROLE = "biometric_role"
    }
}
