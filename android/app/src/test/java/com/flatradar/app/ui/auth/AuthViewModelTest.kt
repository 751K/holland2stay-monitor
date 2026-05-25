package com.flatradar.app.ui.auth

import android.app.Application
import com.flatradar.app.data.local.BiometricAuth
import com.flatradar.app.data.local.TokenManager
import com.flatradar.app.data.remote.ApiClient
import com.flatradar.app.data.remote.ApiError
import com.flatradar.app.data.remote.ApiException
import com.flatradar.app.data.remote.ApiResponse
import com.flatradar.app.data.remote.AuthInterceptor
import com.flatradar.app.data.remote.AuthService
import com.flatradar.app.data.remote.MeService
import com.flatradar.app.data.remote.AccountDeleteResponse
import com.flatradar.app.data.remote.RevokeResponse
import com.flatradar.app.domain.model.LoginResponse
import com.flatradar.app.domain.model.MeResponse
import com.flatradar.app.domain.model.UserInfo
import com.flatradar.app.navigation.NavigationCoordinator
import com.flatradar.app.push.FcmTokenManager
import com.flatradar.app.ui.components.AppErrorBus
import io.mockk.coEvery
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class AuthViewModelTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    private val apiClient = mockk<ApiClient>(relaxed = true)
    private val authService = mockk<AuthService>()
    private val meService = mockk<MeService>()
    private val tokenManager = mockk<TokenManager>(relaxed = true)
    private val biometricAuth = mockk<BiometricAuth>(relaxed = true)
    private val authInterceptor = mockk<AuthInterceptor>(relaxed = true)
    private val navigationCoordinator = mockk<NavigationCoordinator>(relaxed = true)
    private val fcmTokenManager = mockk<FcmTokenManager>(relaxed = true)
    private val errorBus = mockk<AppErrorBus>(relaxed = true)
    private val application = mockk<Application>(relaxed = true)

    @Before
    fun setup() {
        Dispatchers.setMain(testDispatcher)

        every { apiClient.auth } returns authService
        every { apiClient.me } returns meService

        every { biometricAuth.isAvailable() } returns false
        every { biometricAuth.hasStoredUserCredentials() } returns false
        every { biometricAuth.biometryName() } returns "Biometrics"

        every { authInterceptor.authFailures } returns MutableSharedFlow<Unit>().asSharedFlow()
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel() = AuthViewModel(
        apiClient, tokenManager, biometricAuth,
        authInterceptor, navigationCoordinator, fcmTokenManager, errorBus,
        application
    )

    @Test
    fun `login success sets authenticated role and userInfo`() = runTest {
        val vm = createViewModel()
        val loginResp = LoginResponse(
            token = "t", role = "user", user = UserInfo("1", "Alice")
        )
        val meResp = MeResponse("user", UserInfo("1", "Alice"))

        coEvery { authService.login(any()) } returns ApiResponse(ok = true, data = loginResp, error = null)
        coEvery { authService.getMe() } returns ApiResponse(ok = true, data = meResp, error = null)

        vm.loginAsUser("alice", "secret")

        val s = vm.uiState.value
        assert(s.isAuthenticated)
        assert(s.role == "user")
        assert(s.userInfo?.name == "Alice")
        assert(!s.isLoading)

        verify { tokenManager.saveToken("t") }
    }

    @Test
    fun `login failure sets errorMessage`() = runTest {
        val vm = createViewModel()
        coEvery { authService.login(any()) } returns ApiResponse(
            ok = false, data = null, error = ApiError("bad_credentials", "Wrong password")
        )

        vm.loginAsUser("alice", "wrong")

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
        assert(s.errorMessage == "Wrong password")
        assert(!s.isLoading)
    }

    @Test
    fun `login network error sets localized message`() = runTest {
        val vm = createViewModel()
        coEvery { authService.login(any()) } throws ApiException("network", "Connection refused")

        vm.loginAsUser("alice", "secret")

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
        assert(s.errorMessage == "Connection refused")
        assert(!s.isLoading)
    }

    @Test
    fun `enterAsGuest is authenticated with guest role`() = runTest {
        val vm = createViewModel()
        vm.enterAsGuest()

        val s = vm.uiState.value
        assert(s.isAuthenticated)
        assert(s.role == "guest")
        assert(s.userInfo == null)
    }

    @Test
    fun `logout clears auth state calls clearToken`() = runTest {
        val vm = createViewModel()
        vm.enterAsGuest()
        coEvery { authService.logout() } returns ApiResponse(ok = true, data = RevokeResponse(true), error = null)

        vm.logout()

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
        verify { tokenManager.clearToken() }
        verify { navigationCoordinator.reset() }
    }

    @Test
    fun `restoreSession with valid token fetches and applies user`() = runTest {
        every { tokenManager.getToken() } returns "existing-token"
        coEvery { authService.getMe() } returns ApiResponse(
            ok = true, data = MeResponse("user", UserInfo("1", "Bob")), error = null
        )

        val vm = createViewModel()
        vm.restoreSession()

        val s = vm.uiState.value
        assert(s.isAuthenticated)
        assert(s.role == "user")
        assert(s.userInfo?.name == "Bob")
    }

    @Test
    fun `restoreSession with no token does nothing`() = runTest {
        every { tokenManager.getToken() } returns null

        val vm = createViewModel()
        vm.restoreSession()

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
    }

    @Test
    fun `restoreSession with expired token clears auth`() = runTest {
        every { tokenManager.getToken() } returns "expired-token"
        coEvery { authService.getMe() } throws ApiException("unauthorized", "Token expired")

        val vm = createViewModel()
        vm.restoreSession()

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
        verify { tokenManager.clearToken() }
    }

    @Test
    fun `deleteAccount success clears auth and biometric`() = runTest {
        val vm = createViewModel()
        coEvery { meService.deleteAccount() } returns ApiResponse(
            ok = true,
            data = AccountDeleteResponse(true, "1"),
            error = null
        )

        vm.deleteAccount()

        val s = vm.uiState.value
        assert(!s.isAuthenticated)
        verify { tokenManager.clearToken() }
        verify { biometricAuth.deleteCredentials() }
    }

    @Test
    fun `deleteAccount failure sets errorMessage`() = runTest {
        val vm = createViewModel()
        vm.enterAsGuest()
        coEvery { meService.deleteAccount() } returns ApiResponse(
            ok = false,
            data = null,
            error = ApiError("forbidden", "Cannot delete")
        )

        vm.deleteAccount()

        val s = vm.uiState.value
        assert(s.errorMessage == "Cannot delete")
        assert(s.isAuthenticated) // still authenticated — delete failed
    }

    @Test
    fun `register success sets authenticated`() = runTest {
        val vm = createViewModel()
        val regResp = LoginResponse(token = "rt", role = "user", user = UserInfo("2", "New"))
        coEvery { authService.register(any()) } returns ApiResponse(ok = true, data = regResp, error = null)
        coEvery { authService.getMe() } returns ApiResponse(ok = true, data = MeResponse("user", UserInfo("2", "New")), error = null)

        vm.register("new", "pass")

        val s = vm.uiState.value
        assert(s.isAuthenticated)
        assert(s.userInfo?.name == "New")
    }

    @Test
    fun `isAdmin isUser isGuest reflect current role`() = runTest {
        val vm = createViewModel()
        vm.enterAsGuest()
        assert(vm.isGuest)
        assert(!vm.isAdmin)
        assert(!vm.isUser)
    }
}
