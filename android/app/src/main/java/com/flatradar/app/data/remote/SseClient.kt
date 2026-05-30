package com.flatradar.app.data.remote

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * SSE streaming client — matches iOS SSEClient.
 *
 * Uses OkHttp for HTTP streaming, parsing SSE text/event-stream lines.
 * Connecting party (NotificationsViewModel) provides token + baseUrl.
 */
@Singleton
class SseClient @Inject constructor() {

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // No read timeout for streaming
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    /**
     * Connect to the SSE stream, emitting data lines as they arrive.
     *
     * @param baseUrl  Current server base URL (e.g. https://flatradar.app)
     * @param token    Bearer token (null for guest)
     * @param lastId   Last known notification ID for incremental delivery
     */
    fun connect(
        baseUrl: String,
        token: String?,
        lastId: Int
    ): Flow<SseEvent> = callbackFlow {
        val url = "${baseUrl.trimEnd('/')}/api/v1/notifications/stream?last_id=$lastId"
        val requestBuilder = Request.Builder()
            .url(url)
            .header("Accept", "text/event-stream")
            .header("Cache-Control", "no-cache")

        if (!token.isNullOrEmpty()) {
            requestBuilder.header("Authorization", "Bearer $token")
        }

        val call = client.newCall(requestBuilder.build())
        val response = withContext(Dispatchers.IO) { call.execute() }

        if (!response.isSuccessful) {
            throw IOException("SSE connection failed: ${response.code}")
        }

        // 整个 SSE 读取循环必须在 IO 线程执行。
        // callbackFlow 的 producer block 继承 collector 的 dispatcher（
        // viewModelScope.launch 默认 Main），不加 withContext(IO) 会
        // 在主线程上阻塞 readUtf8Line()，导致 ANR。
        withContext(Dispatchers.IO) {
            val source = response.body?.source()
                ?: throw IOException("SSE empty body")

            try {
                while (!source.exhausted()) {
                    val line = source.readUtf8Line() ?: break
                    when {
                        line.startsWith("data:") -> {
                            val payload = line.removePrefix("data:").trimStart()
                            trySend(SseEvent.Data(payload))
                        }
                        line.startsWith("retry:") -> {
                            val ms = line.removePrefix("retry:").trim().toIntOrNull() ?: 2000
                            trySend(SseEvent.Retry(ms))
                        }
                        line.startsWith(":") -> {
                            trySend(SseEvent.Keepalive)
                        }
                    }
                }
            } catch (e: IOException) {
                // Connection closed by server — normal for SSE keepalive rotation
            } finally {
                response.close()
            }
        }

        channel.close()
        awaitClose { call.cancel() }
    }

    sealed class SseEvent {
        data class Data(val payload: String) : SseEvent()
        data class Retry(val ms: Int) : SseEvent()
        data object Keepalive : SseEvent()
    }
}
