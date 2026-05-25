package com.flatradar.app.push

import android.content.Context
import com.flatradar.app.BuildConfig
import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.DeviceRegisterRequest
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.util.Locale
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class FcmTokenManager @Inject constructor(
    private val apiClient: ApiClient,
    private val tokenManager: TokenManager,
    @ApplicationContext private val context: Context
) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun onTokenRefreshed(token: String) {
        prefs.edit().putString(KEY_FCM_TOKEN, token).apply()
        if (tokenManager.getToken() != null) {
            scope.launch { registerDevice(token) }
        }
    }

    fun registerCurrentDevice() {
        val fcmToken = prefs.getString(KEY_FCM_TOKEN, null) ?: return
        scope.launch { registerDevice(fcmToken) }
    }

    fun unregisterCurrentDevice() {
        val deviceId = prefs.getInt(KEY_DEVICE_ID, -1)
        if (deviceId <= 0) return
        prefs.edit().remove(KEY_DEVICE_ID).apply()
        scope.launch {
            try {
                apiClient.devices.deleteDevice(deviceId)
                android.util.Log.i("FcmTokenManager", "device unregistered id=$deviceId")
            } catch (e: Exception) {
                android.util.Log.w("FcmTokenManager", "device unregister failed id=$deviceId", e)
            }
        }
    }

    private suspend fun registerDevice(fcmToken: String) {
        try {
            val resp = apiClient.devices.registerDevice(
                DeviceRegisterRequest(
                    deviceToken = fcmToken,
                    env = if (BuildConfig.DEBUG) "sandbox" else "production",
                    platform = "android",
                    model = "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}",
                    bundleId = BuildConfig.APPLICATION_ID,
                    language = Locale.getDefault().toLanguageTag()
                )
            )
            if (resp.ok && resp.data != null) {
                prefs.edit().putInt(KEY_DEVICE_ID, resp.data.deviceId).apply()
                android.util.Log.i("FcmTokenManager", "device registered id=${resp.data.deviceId}")
            } else {
                android.util.Log.w("FcmTokenManager", "device registration rejected: ${resp.error?.code} ${resp.error?.message}")
            }
        } catch (e: Exception) {
            android.util.Log.e("FcmTokenManager", "device registration failed", e)
        }
    }

    companion object {
        private const val PREFS_NAME = "flatradar_fcm"
        private const val KEY_FCM_TOKEN = "fcm_token"
        private const val KEY_DEVICE_ID = "registered_device_id"
    }
}
