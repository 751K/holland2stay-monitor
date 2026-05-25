package com.flatradar.app.ui.calendar

import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiError
import com.flatradar.app.data.remote.ApiResponse
import com.flatradar.app.data.remote.CalendarResponse
import com.flatradar.app.data.remote.ListingsService
import com.flatradar.app.data.remote.MapCalendarListingDto
import io.mockk.coEvery
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
import java.time.LocalDate
import java.time.YearMonth

@OptIn(ExperimentalCoroutinesApi::class)
class CalendarViewModelTest {

    private val apiClient = mockk<ApiClient>(relaxed = true)
    private val listingsService = mockk<ListingsService>()

    @Before
    fun setup() {
        every { apiClient.listings } returns listingsService
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel() = CalendarViewModel(apiClient)

    private fun makeDto(
        id: String, name: String, availableFrom: String
    ) = MapCalendarListingDto(
        id = id, name = name, status = "Available", availableFrom = availableFrom
    )

    // ── load ─────────────────────────────────────────────────────────

    @Test
    fun `load success populates byDay and listings`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val dto = makeDto("1", "Test Listing", "2026-06-15 00:00:00")
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(listOf(dto)), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.listings.size == 1)
        assert(s.byDay.containsKey("2026-06-15"))
        assert(s.byDay["2026-06-15"]?.size == 1)
    }

    @Test
    fun `load with multiple listings groups by day`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val dto1 = makeDto("1", "A", "2026-06-15 00:00:00")
        val dto2 = makeDto("2", "B", "2026-06-15 00:00:00")
        val dto3 = makeDto("3", "C", "2026-06-20 00:00:00")
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(listOf(dto1, dto2, dto3)), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.listings.size == 3)
        assert(s.byDay["2026-06-15"]?.size == 2)
        assert(s.byDay["2026-06-20"]?.size == 1)
    }

    @Test
    fun `load with invalid availableFrom filters out invalid dates`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        val valid = makeDto("1", "Valid", "2026-07-01 00:00:00")
        val invalid = makeDto("2", "NoDate", "")
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(listOf(valid, invalid)), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.listings.size == 1)
        assert(s.byDay.containsKey("2026-07-01"))
    }

    @Test
    fun `load failure sets errorMessage`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = false, data = null, error = ApiError("error", "Server error")
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.errorMessage == "Server error")
    }

    @Test
    fun `load network error sets errorMessage`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } throws RuntimeException("Network failure")

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.errorMessage != null)
    }

    // ── navigation ───────────────────────────────────────────────────

    @Test
    fun `selectDate updates selectedDate and visibleMonth`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(emptyList()), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        val date = LocalDate.of(2026, 8, 15)
        vm.selectDate(date)

        val s = vm.uiState.value
        assert(s.selectedDate == date)
        assert(s.visibleMonth == YearMonth.of(2026, 8))
    }

    @Test
    fun `previousMonth decrements visibleMonth`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(emptyList()), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        val initial = vm.uiState.value.visibleMonth
        vm.previousMonth()

        assert(vm.uiState.value.visibleMonth == initial.minusMonths(1))
        assert(vm.uiState.value.selectedDate == null)
    }

    @Test
    fun `nextMonth increments visibleMonth`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(emptyList()), error = null
        )
        val vm = createViewModel()
        advanceUntilIdle()

        val initial = vm.uiState.value.visibleMonth
        vm.nextMonth()

        assert(vm.uiState.value.visibleMonth == initial.plusMonths(1))
        assert(vm.uiState.value.selectedDate == null)
    }

    @Test
    fun `load empty response shows no error`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { listingsService.getCalendar() } returns ApiResponse(
            ok = true, data = CalendarResponse(emptyList()), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.listings.isEmpty())
        assert(s.byDay.isEmpty())
    }
}
