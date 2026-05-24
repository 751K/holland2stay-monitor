package com.flatradar.app.di

import android.content.Context
import com.flatradar.app.data.local.PreferencesManager
import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.AuthInterceptor
import com.flatradar.app.data.remote.SseClient
import com.flatradar.app.navigation.NavigationCoordinator
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object AppModule {

    @Provides
    @Singleton
    fun provideTokenManager(@ApplicationContext context: Context): TokenManager {
        return TokenManager(context)
    }

    @Provides
    @Singleton
    fun providePreferencesManager(@ApplicationContext context: Context): PreferencesManager {
        return PreferencesManager(context)
    }

    @Provides
    @Singleton
    fun provideAuthInterceptor(tokenManager: TokenManager): AuthInterceptor {
        return AuthInterceptor(tokenManager)
    }

    @Provides
    @Singleton
    fun provideApiClient(authInterceptor: AuthInterceptor): ApiClient {
        return ApiClient(authInterceptor)
    }

    @Provides
    @Singleton
    fun provideSseClient(): SseClient {
        return SseClient()
    }

    @Provides
    @Singleton
    fun provideNavigationCoordinator(): NavigationCoordinator {
        return NavigationCoordinator()
    }
}
