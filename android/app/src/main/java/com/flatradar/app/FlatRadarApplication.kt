package com.flatradar.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.push.NotificationChannels
import com.flatradar.app.util.CrashReporter
import dagger.hilt.android.HiltAndroidApp

@HiltAndroidApp
class FlatRadarApplication : Application() {

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        CrashReporter.init(ApiClient.DEFAULT_BASE_URL, filesDir)
    }

    private fun createNotificationChannels() {
        val listingsChannel = NotificationChannel(
            NotificationChannels.LISTINGS,
            "New Listings",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Alerts for new and changed listings"
        }

        val generalChannel = NotificationChannel(
            NotificationChannels.GENERAL,
            "General",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "General notifications"
        }

        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(listOf(listingsChannel, generalChannel))
    }
}
