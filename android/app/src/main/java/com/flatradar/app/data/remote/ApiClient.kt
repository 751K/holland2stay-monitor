package com.flatradar.app.data.remote

import com.squareup.moshi.Moshi
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ApiClient @Inject constructor(
    authInterceptor: AuthInterceptor
) {
    lateinit var auth: AuthService
        private set
    lateinit var stats: StatsService
        private set
    lateinit var listings: ListingsService
        private set
    lateinit var notifications: NotificationsService
        private set
    lateinit var me: MeService
        private set
    lateinit var legal: LegalService
        private set
    lateinit var feedback: FeedbackService
        private set
    lateinit var devices: DevicesService
        private set
    lateinit var admin: AdminService
        private set

    var baseUrl: String = DEFAULT_BASE_URL
        private set

    private val moshi: Moshi = Moshi.Builder().build()

    private val okHttpClient: OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(authInterceptor)
        .addInterceptor(
            HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.NONE
            }
        )
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    init {
        createServices(DEFAULT_BASE_URL)
    }

    fun configureBaseUrl(url: String) {
        val clean = url.trimEnd('/')
        if (clean == baseUrl) return
        baseUrl = clean
        createServices(clean)
    }

    private fun createServices(baseUrl: String) {
        val normalizedUrl = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        val retrofit = Retrofit.Builder()
            .baseUrl(normalizedUrl)
            .client(okHttpClient)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()

        auth = retrofit.create(AuthService::class.java)
        stats = retrofit.create(StatsService::class.java)
        listings = retrofit.create(ListingsService::class.java)
        notifications = retrofit.create(NotificationsService::class.java)
        me = retrofit.create(MeService::class.java)
        legal = retrofit.create(LegalService::class.java)
        feedback = retrofit.create(FeedbackService::class.java)
        devices = retrofit.create(DevicesService::class.java)
        admin = retrofit.create(AdminService::class.java)
    }

    companion object {
        const val DEFAULT_BASE_URL = "https://flatradar.app"
    }
}

class ApiException(
    val code: String,
    override val message: String
) : Exception(message) {
    val isAuthError: Boolean
        get() = code == "unauthorized" || code == "forbidden"

    companion object {
        /**
         * 从 Retrofit HttpException 的 error body 里提取错误信息。
         * 服务端返回 {"ok":false, "error":{"code":"...", "message":"..."}}
         */
        fun fromHttpException(e: retrofit2.HttpException): ApiException {
            return try {
                val body = e.response()?.errorBody()?.string() ?: ""
                val json = org.json.JSONObject(body)
                val errObj = json.optJSONObject("error")
                if (errObj != null) {
                    ApiException(
                        errObj.optString("code", "http_${e.code()}"),
                        errObj.optString("message", "请求失败 (${e.code()})")
                    )
                } else {
                    ApiException("http_${e.code()}", "请求失败 (${e.code()})")
                }
            } catch (_: Exception) {
                ApiException("http_${e.code()}", "请求失败 (${e.code()})")
            }
        }
    }
}
