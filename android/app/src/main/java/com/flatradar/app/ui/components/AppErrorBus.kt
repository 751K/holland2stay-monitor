package com.flatradar.app.ui.components

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class AppErrorBus @Inject constructor() {
    private val _messages = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val messages = _messages.asSharedFlow()

    fun show(message: String?) {
        val clean = message?.trim().orEmpty()
        if (clean.isNotBlank()) {
            _messages.tryEmit(clean)
        }
    }
}
