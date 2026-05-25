# FlatRadar ProGuard Rules

# Retrofit + Moshi
-keepattributes Signature
-keepattributes *Annotation*
-keep class com.flatradar.app.data.remote.ApiModels** { *; }
-keep class com.flatradar.app.domain.model.** { *; }

# Moshi
-dontwarn com.squareup.moshi.**
-keep class com.squareup.moshi.** { *; }

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**

# Hilt
-keep class dagger.hilt.** { *; }

# Firebase Messaging
-keep class com.google.firebase.** { *; }
-dontwarn com.google.firebase.**
-keep class com.flatradar.app.push.FcmService { *; }
