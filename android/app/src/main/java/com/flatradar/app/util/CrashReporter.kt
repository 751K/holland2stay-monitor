package com.flatradar.app.util

import android.os.Build
import android.util.Log
import com.flatradar.app.BuildConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * 全局未捕获异常处理器。
 *
 * 对标 iOS CrashDiagnosticsCollector：
 * - 捕获未处理异常，收集堆栈 + 设备信息
 * - 尝试 POST 到 /api/v1/diagnostics/crash（bearer_optional，无需登录）
 * - 同时写入本地文件兜底（data/files/flatradar_crashes/）
 */
object CrashReporter {

    private const val TAG = "CrashReporter"
    private const val CRASH_ENDPOINT = "/api/v1/diagnostics/crash"
    private const val MAX_PAYLOAD_BYTES = 200_000  // 后端上限 256KB，留余量

    private val defaultHandler: Thread.UncaughtExceptionHandler? =
        Thread.getDefaultUncaughtExceptionHandler()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private var baseUrl: String = "https://flatradar.app"
    private var localDir: File? = null

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    fun init(appBaseUrl: String, filesDir: File) {
        baseUrl = appBaseUrl.trimEnd('/')
        localDir = File(filesDir, "flatradar_crashes").also { it.mkdirs() }

        val current = Thread.getDefaultUncaughtExceptionHandler()
        if (current is CrashHandler) return  // 防重复注册

        Thread.setDefaultUncaughtExceptionHandler(
            CrashHandler(defaultHandler ?: current)
        )
        Log.i(TAG, "crash handler registered, endpoint=${baseUrl}$CRASH_ENDPOINT")
    }

    /** Update server URL after user changes it in Settings. */
    fun updateBaseUrl(newUrl: String) {
        baseUrl = newUrl.trimEnd('/')
    }

    // ── 异常处理器 ─────────────────────────────────────────────────

    private class CrashHandler(
        private val fallback: Thread.UncaughtExceptionHandler
    ) : Thread.UncaughtExceptionHandler {

        override fun uncaughtException(thread: Thread, ex: Throwable) {
            try {
                val payload = buildPayload(thread, ex)
                saveToFile(payload)
                sendToServer(payload)
            } catch (_: Exception) {
                // 最坏情况：报告本身崩了，不能吞掉原始异常
            } finally {
                fallback.uncaughtException(thread, ex)
            }
        }
    }

    // ── Payload 构造 ──────────────────────────────────────────────

    private fun buildPayload(thread: Thread, ex: Throwable): JSONObject {
        val sw = StringWriter()
        ex.printStackTrace(PrintWriter(sw))

        return JSONObject().apply {
            put("kind", if (ex is OutOfMemoryError) "diskwrite" else "crash")
            put("platform", "android")
            put("app_version", "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
            put("os_version", Build.VERSION.RELEASE)
            put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("payload", JSONObject().apply {
                put("thread", thread.name)
                put("exception", ex.javaClass.name)
                put("message", ex.message ?: "")
                put("stacktrace", sw.toString())
                put("timestamp", SimpleDateFormat(
                    "yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US
                ).format(Date()))
                // 递归收集 cause chain
                var cause = ex.cause
                var depth = 0
                val causes = org.json.JSONArray()
                while (cause != null && depth < 8) {
                    val csw = StringWriter()
                    cause.printStackTrace(PrintWriter(csw))
                    causes.put(JSONObject().apply {
                        put("exception", cause.javaClass.name)
                        put("message", cause.message ?: "")
                        put("stacktrace", csw.toString())
                    })
                    cause = cause.cause
                    depth++
                }
                if (causes.length() > 0) {
                    put("causes", causes)
                }
            })
        }
    }

    // ── 网络发送 ──────────────────────────────────────────────────

    private fun sendToServer(payload: JSONObject) {
        scope.launch {
            try {
                val json = payload.toString()
                if (json.length > MAX_PAYLOAD_BYTES) {
                    Log.w(TAG, "crash payload too large (${json.length} bytes), truncating")
                    // 用截断版重试：只保留主异常，丢弃 cause chain
                    val slim = JSONObject(payload.toString())
                    slim.getJSONObject("payload")?.remove("causes")
                    val slimJson = slim.toString()
                    if (slimJson.length > MAX_PAYLOAD_BYTES) {
                        Log.e(TAG, "crash payload still too large after trim, skipping upload")
                        return@launch
                    }
                    doSend(slimJson)
                    return@launch
                }
                doSend(json)
            } catch (e: Exception) {
                Log.e(TAG, "failed to send crash report", e)
            }
        }
    }

    private fun doSend(json: String) {
        val body = json.toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("$baseUrl$CRASH_ENDPOINT")
            .post(body)
            .header("User-Agent", "FlatRadar-Android/${BuildConfig.VERSION_NAME}")
            .build()
        val response = httpClient.newCall(request).execute()
        if (response.isSuccessful) {
            Log.i(TAG, "crash report uploaded (${json.length} bytes)")
        } else {
            Log.w(TAG, "crash report upload failed: ${response.code}")
        }
        response.close()
    }

    // ── 本地兜底 ──────────────────────────────────────────────────

    private fun saveToFile(payload: JSONObject) {
        try {
            val dir = localDir ?: return
            val ts = SimpleDateFormat("yyyyMMdd'T'HHmmss", Locale.US).format(Date())
            val file = File(dir, "crash-$ts-${payload.optString("kind", "crash")}.json")
            file.writeText(payload.toString(2), Charsets.UTF_8)
            Log.i(TAG, "crash report saved locally: ${file.name}")
        } catch (e: Exception) {
            Log.e(TAG, "failed to save crash locally", e)
        }
    }
}
