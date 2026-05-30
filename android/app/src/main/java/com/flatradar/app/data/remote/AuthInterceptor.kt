package com.flatradar.app.data.remote

import com.flatradar.app.data.local.TokenManager
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import okhttp3.Interceptor
import okhttp3.Response
import javax.inject.Inject
import javax.inject.Singleton

/**
 * OkHttp Interceptor: inject Bearer token + emit auth-failure events on 401/403.
 * Matches iOS APIClient.authFailedNotification + automatic logout.
 */
@Singleton
class AuthInterceptor @Inject constructor(
    private val tokenManager: TokenManager
) : Interceptor {

    private val _authFailures = MutableSharedFlow<Unit>(extraBufferCapacity = 1)
    val authFailures = _authFailures.asSharedFlow()

    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()

        // Inject Bearer token if available
        val token = tokenManager.getToken()
        val request = if (token != null) {
            original.newBuilder()
                .header("Authorization", "Bearer $token")
                .build()
        } else {
            original
        }

        val response = chain.proceed(request)

        // Emit auth failure event on 401/403
        if (response.code == 401 || response.code == 403) {
            _authFailures.tryEmit(Unit)
        }

        return response
    }
}
