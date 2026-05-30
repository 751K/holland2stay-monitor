package com.flatradar.app.data.remote

import com.flatradar.app.domain.model.*
import okhttp3.ResponseBody
import retrofit2.http.*

/**
 * Retrofit service interfaces for all /api/v1/ endpoints.
 * Matches iOS APIClient methods.
 */
interface AuthService {
    @POST("api/v1/auth/login")
    suspend fun login(@Body body: LoginRequest): ApiResponse<LoginResponse>

    @POST("api/v1/auth/register")
    suspend fun register(@Body body: LoginRequest): ApiResponse<LoginResponse>

    @POST("api/v1/auth/logout")
    suspend fun logout(): ApiResponse<RevokeResponse>

    @GET("api/v1/auth/me")
    suspend fun getMe(): ApiResponse<MeResponse>

    @POST("api/v1/auth/password")
    suspend fun changePassword(@Body body: ChangePasswordRequest): ApiResponse<ChangePasswordResponse>
}

interface StatsService {
    @GET("api/v1/stats/public/summary")
    suspend fun getPublicSummary(): ApiResponse<MonitorStatus>

    @GET("api/v1/stats/public/charts")
    suspend fun getPublicCharts(): ApiResponse<ChartKeysResponse>

    @GET("api/v1/stats/public/charts/{key}")
    suspend fun getPublicChart(
        @Path("key") key: String,
        @Query("days") days: Int = 30
    ): ApiResponse<ChartData>
}

interface ListingsService {
    @GET("api/v1/listings")
    suspend fun getListings(
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0,
        @Query("city") city: String? = null,
        @Query("cities") cities: String? = null,
        @Query("status") status: String? = null,
        @Query("q") query: String? = null,
        @Query("sources") sources: String? = null,
        @Query("types") types: String? = null,
        @Query("contract") contract: String? = null,
        @Query("energy") energy: String? = null
    ): ApiResponse<ListingsResponse>

    @GET("api/v1/listings/{id}")
    suspend fun getListing(@Path("id") id: String): ApiResponse<Listing>

    @GET("api/v1/map")
    suspend fun getMap(): ApiResponse<MapResponse>

    @GET("api/v1/calendar")
    suspend fun getCalendar(): ApiResponse<CalendarResponse>
}

interface NotificationsService {
    @GET("api/v1/notifications")
    suspend fun getNotifications(
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0
    ): ApiResponse<NotificationsResponse>

    @POST("api/v1/notifications/read")
    suspend fun markRead(@Body body: MarkReadRequest): ApiResponse<MarkReadResponse>
}

interface LegalService {
    @GET("api/v1/legal")
    suspend fun getLegal(@Query("lang") lang: String? = null): ApiResponse<LegalResponse>
}

interface MeService {
    @GET("api/v1/me/summary")
    suspend fun getMeSummary(): ApiResponse<MeSummaryResponse>

    @GET("api/v1/me/filter")
    suspend fun getMeFilter(): ApiResponse<MeFilterResponse>

    @PUT("api/v1/me/filter")
    suspend fun updateMeFilter(@Body filter: ListingFilter): ApiResponse<MeFilterResponse>

    @DELETE("api/v1/me")
    suspend fun deleteAccount(): ApiResponse<AccountDeleteResponse>

    @GET("api/v1/me/export")
    suspend fun exportMe(): ResponseBody

    @GET("api/v1/filter/options")
    suspend fun getFilterOptions(): ApiResponse<FilterOptionsResponse>
}

interface FeedbackService {
    @POST("api/v1/feedback")
    suspend fun submitFeedback(@Body body: FeedbackRequest): ApiResponse<FeedbackResponse>
}

interface DevicesService {
    @POST("api/v1/devices/register")
    suspend fun registerDevice(@Body body: DeviceRegisterRequest): ApiResponse<DeviceRegisterResponse>

    @GET("api/v1/devices")
    suspend fun getDevices(): ApiResponse<DevicesResponse>

    @DELETE("api/v1/devices/{id}")
    suspend fun deleteDevice(@Path("id") id: Int): ApiResponse<DeviceDeleteResponse>

    @POST("api/v1/devices/test")
    suspend fun testDevice(@Body body: DeviceTestRequest): ApiResponse<DeviceTestResponse>
}

interface AdminService {
    @GET("api/v1/admin/users")
    suspend fun getUsers(): ApiResponse<AdminUsersResponse>

    @POST("api/v1/admin/users/{id}/toggle")
    suspend fun toggleUser(@Path("id") id: String): ApiResponse<AdminUserToggleResponse>

    @DELETE("api/v1/admin/users/{id}")
    suspend fun deleteUser(@Path("id") id: String): ApiResponse<AdminUserDeleteResponse>

    @GET("api/v1/admin/monitor/status")
    suspend fun getMonitorStatus(): ApiResponse<AdminMonitorStatus>

    @POST("api/v1/admin/monitor/start")
    suspend fun startMonitor(): ApiResponse<AdminMonitorActionResponse>

    @POST("api/v1/admin/monitor/stop")
    suspend fun stopMonitor(): ApiResponse<AdminMonitorActionResponse>

    @POST("api/v1/admin/monitor/reload")
    suspend fun reloadMonitor(): ApiResponse<AdminMonitorActionResponse>
}
