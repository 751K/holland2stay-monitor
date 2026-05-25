package com.flatradar.app.ui.notifications

import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiResponse
import com.flatradar.app.data.remote.NotificationsResponse
import com.flatradar.app.data.remote.NotificationsService
import com.flatradar.app.data.remote.SseClient
import com.flatradar.app.domain.model.NotificationItem
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class NotificationsViewModelTest {

    private val apiClient = mockk<ApiClient>(relaxed = true)
    private val notificationsService = mockk<NotificationsService>()
    private val sseClient = mockk<SseClient>()
    private val tokenManager = mockk<TokenManager>(relaxed = true)

    /** A flow that suspends forever on [collect] — used to stop the SSE reconnect loop. */
    private val pending = callbackFlow<Nothing> { awaitClose { } }

    @Before
    fun setup() {
        every { apiClient.notifications } returns notificationsService
        every { apiClient.baseUrl } returns "https://flatradar.app"
        every { tokenManager.getToken() } returns null
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel() = NotificationsViewModel(apiClient, sseClient, tokenManager)

    private fun makeItem(id: Int, read: Int = 0, title: String = "Test $id") = NotificationItem(
        id = id, createdAt = "2026-06-01T12:00:00", type = "new_listing",
        title = title, body = "Body $id", url = "", listingId = "", read = read
    )

    // ── load ─────────────────────────────────────────────────────────

    @Test
    fun `load success populates items and unreadCount`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true, data = NotificationsResponse(listOf(makeItem(1, 0), makeItem(2, 0)), 2, 2, 100, 0), error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returns pending

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.isLive)
        assert(s.items.size == 2)
        assert(s.unreadCount == 2)
    }

    @Test
    fun `load empty notifications`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true, data = NotificationsResponse(emptyList(), 0, 0, 100, 0), error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returns pending

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.items.isEmpty())
        assert(s.unreadCount == 0)
    }

    @Test
    fun `load failure shows error state`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } throws RuntimeException()
        coEvery { sseClient.connect(any(), any(), any()) } returns pending

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(!s.isLoading)
        assert(s.items.isEmpty())
    }

    // ── markRead ─────────────────────────────────────────────────────

    @Test
    fun `markRead marks item and decreases unreadCount`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true,
            data = NotificationsResponse(listOf(makeItem(1, 0), makeItem(2, 0)), 2, 2, 100, 0),
            error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returns pending
        coEvery { notificationsService.markRead(any()) } returns ApiResponse(
            ok = true, data = com.flatradar.app.data.remote.MarkReadResponse(true), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.markRead(1)
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.unreadCount == 1)
        assert(s.items.first { it.id == 1 }.isRead)
        assert(!s.items.first { it.id == 2 }.isRead)
    }

    @Test
    fun `markRead already read item is no-op`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true,
            data = NotificationsResponse(listOf(makeItem(1, 1)), 1, 0, 100, 0),
            error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returns pending

        val vm = createViewModel()
        advanceUntilIdle()

        vm.markRead(1) // already read

        coVerify(exactly = 0) { notificationsService.markRead(any()) }
    }

    @Test
    fun `markAllRead sets unreadCount to zero`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true,
            data = NotificationsResponse(listOf(makeItem(1, 0), makeItem(2, 0), makeItem(3, 0)), 3, 3, 100, 0),
            error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returns pending
        coEvery { notificationsService.markRead(any()) } returns ApiResponse(
            ok = true, data = com.flatradar.app.data.remote.MarkReadResponse(true), error = null
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.markAllRead()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.unreadCount == 0)
        assert(s.items.all { it.isRead })
    }

    // ── SSE merge ────────────────────────────────────────────────────

    @Test
    fun `SSE Data merges new items at top`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true,
            data = NotificationsResponse(listOf(makeItem(1, 0, "Existing")), 1, 1, 100, 0),
            error = null
        )
        val newItemJson = """{"id":2,"created_at":"2026-06-02T12:00:00","type":"status_change","title":"New Item","body":"Hello","url":"","listing_id":"x","read":0}"""
        coEvery { sseClient.connect(any(), any(), any()) } returnsMany listOf(
            flowOf(SseClient.SseEvent.Data("[$newItemJson]")),
            pending
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.items.size == 2)
        assert(s.items[0].id == 2) // new at top
        assert(s.items[1].id == 1)
        assert(s.unreadCount == 2)
    }

    @Test
    fun `SSE Data updates read status on existing items`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true,
            data = NotificationsResponse(listOf(makeItem(1, 0, "Existing")), 1, 1, 100, 0),
            error = null
        )
        val updatedJson = """{"id":1,"created_at":"2026-06-01T12:00:00","type":"new_listing","title":"Existing","body":"Body 1","url":"","listing_id":"","read":1}"""
        coEvery { sseClient.connect(any(), any(), any()) } returnsMany listOf(
            flowOf(SseClient.SseEvent.Data(updatedJson)),
            pending
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val s = vm.uiState.value
        assert(s.items.size == 1)
        assert(s.items[0].isRead)
        assert(s.unreadCount == 0)
    }

    @Test
    fun `SSE Keepalive sets isLive true`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true, data = NotificationsResponse(emptyList(), 0, 0, 100, 0), error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } returnsMany listOf(
            flowOf(SseClient.SseEvent.Keepalive),
            pending
        )

        val vm = createViewModel()
        advanceUntilIdle()

        assert(vm.uiState.value.isLive)
    }

    @Test
    fun `SSE error sets isLive false`() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        coEvery { notificationsService.getNotifications(any(), any()) } returns ApiResponse(
            ok = true, data = NotificationsResponse(emptyList(), 0, 0, 100, 0), error = null
        )
        coEvery { sseClient.connect(any(), any(), any()) } throws RuntimeException("connection lost") andThen pending

        val vm = createViewModel()
        advanceUntilIdle()

        assert(!vm.uiState.value.isLive)
    }
}
