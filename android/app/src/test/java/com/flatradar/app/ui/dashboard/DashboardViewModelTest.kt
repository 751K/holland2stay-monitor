package com.flatradar.app.ui.dashboard

import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiException
import com.flatradar.app.data.remote.ApiResponse
import com.flatradar.app.data.remote.ListingsResponse
import com.flatradar.app.data.remote.ListingsService
import com.flatradar.app.data.remote.MeService
import com.flatradar.app.data.remote.MeSummaryResponse
import com.flatradar.app.data.remote.StatsService
import com.flatradar.app.domain.model.ChartData
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.domain.model.MonitorStatus
import com.flatradar.app.ui.components.AppErrorBus
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class DashboardViewModelTest {

    private val apiClient = mockk<ApiClient>(relaxed = true)
    private val statsService = mockk<StatsService>()
    private val meService = mockk<MeService>()
    private val listingsService = mockk<ListingsService>()
    private val errorBus = mockk<AppErrorBus>(relaxed = true)

    @Before
    fun setup() {
        every { apiClient.stats } returns statsService
        every { apiClient.me } returns meService
        every { apiClient.listings } returns listingsService
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel() = DashboardViewModel(apiClient, errorBus)

    private suspend fun mockGuestCharts() {
        coEvery { statsService.getPublicChart(any(), any()) } returns ApiResponse(
            ok = true, data = ChartData("test", 30, emptyList()), error = null
        )
    }

    // ── Guest ────────────────────────────────────────────────────────

    @Test
    fun `fetchAll guest loads summary and charts but not me`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val vm = createViewModel()

        coEvery { statsService.getPublicSummary() } returns ApiResponse(
            ok = true, data = MonitorStatus(100, 5, 20, 3, "2024-01-01"), error = null
        )
        mockGuestCharts()

        vm.fetchAll(isUser = false)
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.summary?.total == 100)
        assert(s.summary?.new24h == 5)
        assert(s.meSummary == null)
        assert(s.matchPreviews.isEmpty())

        coVerify(exactly = 1) { statsService.getPublicSummary() }
        coVerify(exactly = 0) { meService.getMeSummary() }
        coVerify(exactly = 0) { listingsService.getListings(any(), any()) }
    }

    // ── User ─────────────────────────────────────────────────────────

    @Test
    fun `fetchAll user loads meSummary and matchPreviews`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val vm = createViewModel()

        coEvery { statsService.getPublicSummary() } returns ApiResponse(
            ok = true, data = MonitorStatus(80, 3, 10, 1, "2024-02-01"), error = null
        )
        mockGuestCharts()
        coEvery { meService.getMeSummary() } returns ApiResponse(
            ok = true,
            data = MeSummaryResponse("user", 80, 3, 10, null, "2024-02-01", true),
            error = null
        )
        coEvery { listingsService.getListings(any(), any()) } returns ApiResponse(
            ok = true, data = ListingsResponse(
                listOf(Listing(id = "a", name = "Test", status = "Available")),
                total = 1, limit = 5, offset = 0
            ), error = null
        )

        vm.fetchAll(isUser = true)
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.meSummary?.role == "user")
        assert(s.meSummary?.matchedTotal == 10)
        assert(s.matchPreviews.size == 1)

        coVerify(exactly = 1) { meService.getMeSummary() }
        coVerify(exactly = 1) { listingsService.getListings(any(), any()) }
    }

    // ── Error handling ───────────────────────────────────────────────

    @Test
    fun `fetchAll error sets errorMessage`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val vm = createViewModel()

        coEvery { statsService.getPublicSummary() } throws ApiException("down", "Server unavailable")

        vm.fetchAll(isUser = false)
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.errorMessage != null)
    }

    @Test
    fun `fetchAll meSummary failure does not block dashboard`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val vm = createViewModel()

        coEvery { statsService.getPublicSummary() } returns ApiResponse(
            ok = true, data = MonitorStatus(50, 1, 2, 0, "now"), error = null
        )
        mockGuestCharts()
        coEvery { meService.getMeSummary() } throws ApiException("auth", "Unauthorized")
        coEvery { listingsService.getListings(any(), any()) } returns ApiResponse(
            ok = true, data = ListingsResponse(emptyList(), 0, 5, 0), error = null
        )

        vm.fetchAll(isUser = true)
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.summary != null)
        assert(s.meSummary == null) // silently failed
        assert(s.errorMessage == null)
    }

    // ── clear ────────────────────────────────────────────────────────

    @Test
    fun `clear resets all state`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val vm = createViewModel()

        coEvery { statsService.getPublicSummary() } returns ApiResponse(
            ok = true, data = MonitorStatus(99, 9, 9, 9, "z"), error = null
        )
        mockGuestCharts()

        vm.fetchAll(isUser = false)
        advanceUntilIdle()
        vm.clear()

        val s = vm.uiState.value
        assert(s.summary == null)
        assert(s.chartDailyNew == null)
        assert(!s.isLoading)
    }

    // ── initial state ────────────────────────────────────────────────

    @Test
    fun `initial state is empty and not loading`() {
        Dispatchers.setMain(StandardTestDispatcher())
        val vm = createViewModel()
        val s = vm.uiState.value
        assert(s.summary == null)
        assert(s.meSummary == null)
        assert(s.matchPreviews.isEmpty())
        assert(!s.isLoading)
        assert(s.errorMessage == null)
    }
}
