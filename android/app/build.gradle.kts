plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.hilt)
    alias(libs.plugins.ksp)
    alias(libs.plugins.google.services)
}

import java.util.Properties

val localProperties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) {
        file.inputStream().use(::load)
    }
}

// Maps API key: local.properties > env > empty (app degrades gracefully)
val mapsApiKey = localProperties.getProperty("MAPS_API_KEY")
    ?: System.getenv("MAPS_API_KEY")
    ?: ""

// Version: env APP_VERSION (CI injects git tag like "1.7.10") > local.properties > hardcoded fallback
val appVersionName = System.getenv("APP_VERSION")
    ?: localProperties.getProperty("APP_VERSION")
    ?: "1.7.10"

// versionCode: env VERSION_CODE > derive from versionName (e.g. 1.7.10 → 1710) > hardcoded fallback
val appVersionCode = System.getenv("VERSION_CODE")?.toIntOrNull()
    ?: localProperties.getProperty("VERSION_CODE")?.toIntOrNull()
    ?: run {
        // Parse major.minor.patch → major*1000 + minor*100 + patch (clamped to 2 digits each)
        val parts = appVersionName.split(".").map { it.toIntOrNull() ?: 0 }
        val major = parts.getOrElse(0) { 1 }.coerceIn(0, 99)
        val minor = parts.getOrElse(1) { 0 }.coerceIn(0, 99)
        val patch = parts.getOrElse(2) { 0 }.coerceIn(0, 9)  // single digit to stay < 2100000000
        major * 1000 + minor * 100 + patch
    }

// Signing: env vars (CI) > local.properties (dev) > empty fallback (debug-only)
val releaseStoreFile = localProperties.getProperty("RELEASE_STORE_FILE")
    ?: System.getenv("RELEASE_STORE_FILE")
    ?: "sign.p12"
val releaseStorePassword = localProperties.getProperty("RELEASE_STORE_PASSWORD")
    ?: System.getenv("RELEASE_STORE_PASSWORD")
    ?: System.getenv("ANDROID_STORE_PASSWORD")
    ?: ""
val releaseKeyAlias = localProperties.getProperty("RELEASE_KEY_ALIAS")
    ?: System.getenv("RELEASE_KEY_ALIAS")
    ?: System.getenv("ANDROID_KEY_ALIAS")
    ?: "flatradar"
val releaseKeyPassword = localProperties.getProperty("RELEASE_KEY_PASSWORD")
    ?: System.getenv("RELEASE_KEY_PASSWORD")
    ?: System.getenv("ANDROID_KEY_PASSWORD")
    ?: ""

android {
    namespace = "com.flatradar.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.flatradar.app"
        minSdk = 31
        targetSdk = 35
        versionCode = appVersionCode
        versionName = appVersionName

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        manifestPlaceholders["mapsApiKey"] = mapsApiKey
        buildConfigField("String", "MAPS_API_KEY", "\"${mapsApiKey.replace("\\", "\\\\").replace("\"", "\\\"")}\"")
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        create("release") {
            storeFile = file(releaseStoreFile)
            storePassword = releaseStorePassword
            keyAlias = releaseKeyAlias
            keyPassword = releaseKeyPassword
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    // Compose BOM
    val composeBom = platform(libs.compose.bom)
    implementation(composeBom)
    implementation(libs.compose.material3)
    implementation(libs.compose.material3.windowsizeclass)
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material.icons)
    debugImplementation(libs.compose.ui.tooling)
    // debugImplementation(libs.compose.ui.test.manifest)

    // Navigation
    implementation(libs.compose.navigation)

    // Activity + Lifecycle
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.viewmodel.compose)

    // Hilt
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.hilt.navigation.compose)

    // Network
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)
    implementation(libs.retrofit)
    implementation(libs.retrofit.moshi)
    implementation(libs.moshi)
    ksp(libs.moshi.kotlin)

    // Coroutines
    implementation(libs.kotlinx.coroutines.android)

    // DataStore + Security
    implementation(libs.datastore)
    implementation(libs.security.crypto)

    // Coil
    implementation(libs.coil)

    // Vico charts
    implementation(libs.vico.compose.m3)

    // Maps
    implementation(libs.maps.compose)
    implementation(libs.maps.compose.utils)
    implementation(libs.play.services.maps)
    implementation(libs.play.services.location)

    // Firebase
    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging)

    // Core
    implementation(libs.core.ktx)

    // Test
    testImplementation(libs.junit)
    testImplementation(libs.mockk)
    testImplementation(libs.coroutines.test)
    // androidTestImplementation(libs.compose.ui.test)
}
