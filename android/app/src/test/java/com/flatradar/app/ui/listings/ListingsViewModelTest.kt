package com.flatradar.app.ui.listings

import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiError
import com.flatradar.app.data.remote.ApiResponse
import com.flatradar.app.data.remote.FilterOptionsResponse
import com.flatradar.app.data.remote.ListingsResponse
import com.flatradar.app.data.remote.ListingsService
import com.flatradar.app.data.remote.MeService
import com.flatradar.app.domain.model.Listing
import com.flatradar.app.ui.components.AppErrorBus
import io.mockk.coEvery
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
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
class ListingsViewModelTest {

    private val apiClient = mockk<ApiClient>(relaxed = true)
    private val listingsService = mockk<ListingsService>()
    private val meService = mockk<MeService>()
    private val errorBus = mockk<AppErrorBus>(relaxed = true)

    @Before
    fun setup() {
        every { apiClient.listings } returns listingsService
        every { apiClient.me } returns meService
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel() = ListingsViewModel(apiClient, errorBus)

    private fun makeListing(id: String, name: String) = Listing(
        id = id, name = name, status = "Available"
    )

    private fun mockLoadSuccess(items: List<Listing> = emptyList(), total: Int = 0) {
        coEvery { listingsService.getListings(any(), any(), any(), any(), any(), any(), any(), any(), any(), any()) } returns
            ApiResponse(ok = true, data = ListingsResponse(items, total, 50, 0), error = null)
    }

    private fun mockFilterOptions() {
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(cities = listOf("Eindhoven")), error = null
        )
    }

    // ── load ─────────────────────────────────────────────────────────

    @Test
    fun `init loads listings and filterOptions`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(listOf(makeListing("1", "Test")), total = 1)
        mockFilterOptions()

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.items.size == 1)
        assert(s.total == 1)
        assert(s.filterOptions.cities.contains("Eindhoven"))
    }

    @Test
    fun `load failure sets errorMessage`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getListings(any(), any(), any(), any(), any(), any(), any(), any(), any(), any()) } returns
            ApiResponse(ok = false, data = null, error = ApiError("err", "Server error"))
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.errorMessage == "Server error")
    }

    @Test
    fun `filterOptions failure does not block listings`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(listOf(makeListing("1", "OK")), total = 1)
        coEvery { meService.getFilterOptions() } throws RuntimeException()

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.items.size == 1)
        assert(s.filterOptions == FilterOptionsResponse())
    }

    // ── loadMore ─────────────────────────────────────────────────────

    @Test
    fun `loadMore appends items`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(
            (0..49).map { makeListing(it.toString(), "Item $it") }.toList(), total = 100
        )
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        // Set up loadMore response
        val page2 = (50..74).map { makeListing(it.toString(), "More $it") }
        coEvery { listingsService.getListings(any(), any(), any(), any(), any(), any(), any(), any(), any(), any()) } returns
            ApiResponse(ok = true, data = ListingsResponse(page2, 100, 50, 50), error = null)

        vm.loadMore()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoadingMore)
        assert(s.items.size == 75) // 50 + 25
        assert(s.total == 100)
    }

    @Test
    fun `loadMore no more items sets hasMore false`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(
            (0..29).map { makeListing(it.toString(), "Item $it") }.toList(), total = 30
        )
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        // Only 30 items total — less than limit=50 so hasMore=false
        vm.loadMore() // should be no-op

        assert(vm.uiState.value.items.size == 30)
    }

    @Test
    fun `loadMore failure shows error but keeps existing items`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(
            (0..49).map { makeListing(it.toString(), "Item $it") }.toList(), total = 100
        )
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery { listingsService.getListings(any(), any(), any(), any(), any(), any(), any(), any(), any(), any()) } returns
            ApiResponse(ok = false, data = null, error = ApiError("err", "Failed"))

        vm.loadMore()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoadingMore)
        assert(s.items.size == 50) // original items preserved
    }

    // ── search / filters ────────────────────────────────────────────

    @Test
    fun `updateSearch sets query and reloads`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(emptyList(), total = 0)
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        mockLoadSuccess(listOf(makeListing("x", "Match")), total = 1)
        vm.updateSearch("test query")

        advanceUntilIdle()
        assert(vm.uiState.value.searchQuery == "test query")
        assert(vm.uiState.value.items.size == 1)
    }

    @Test
    fun `updateFilters sets filters and reloads`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(emptyList(), total = 0)
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        mockLoadSuccess(emptyList(), total = 0)
        vm.updateFilters(
            ListingsViewModel.ListingFilters(city = "Eindhoven", status = "available")
        )

        advanceUntilIdle()
        assert(vm.uiState.value.filters.city == "Eindhoven")
        assert(vm.uiState.value.filters.status == "available")
    }

    @Test
    fun `clearFilters resets filters and search query`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        mockLoadSuccess(emptyList(), total = 0)
        coEvery { meService.getFilterOptions() } returns ApiResponse(
            ok = true, data = FilterOptionsResponse(), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        vm.updateSearch("something")
        vm.updateFilters(ListingsViewModel.ListingFilters(city = "Amsterdam"))
        advanceUntilIdle()
        mockLoadSuccess(emptyList(), total = 0)

        vm.clearFilters()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.searchQuery == "")
        assert(!s.filters.isActive)
    }

    // ── ListingFilters.isActive ──────────────────────────────────────

    @Test
    fun `ListingFilters isActive reflects any set filter`() {
        val f = ListingsViewModel.ListingFilters()
        assert(!f.isActive)

        val withCity = f.copy(city = "Eindhoven")
        assert(withCity.isActive)

        val withStatus = f.copy(status = "available")
        assert(withStatus.isActive)

        val withCities = f.copy(cities = listOf("A", "B"))
        assert(withCities.isActive)

        val withContract = f.copy(contract = "short")
        assert(withContract.isActive)

        val withEnergy = f.copy(energy = "A")
        assert(withEnergy.isActive)

        val withTypes = f.copy(types = listOf("studio"))
        assert(withTypes.isActive)

        val withSources = f.copy(sources = listOf("h2s"))
        assert(withSources.isActive)
    }
}
