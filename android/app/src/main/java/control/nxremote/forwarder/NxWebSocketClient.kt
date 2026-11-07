package control.nxremote.forwarder

import android.net.Uri
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.net.InetAddress
import java.net.Socket
import javax.net.SocketFactory

/** A SocketFactory that disables Nagle's algorithm on every socket it creates. */
object NoDelaySocketFactory : SocketFactory() {
    private fun tune(s: Socket): Socket = s.apply {
        try { tcpNoDelay = true } catch (_: Exception) {}
    }
    override fun createSocket(): Socket = tune(Socket())
    override fun createSocket(host: String?, port: Int): Socket = tune(Socket(host, port))
    override fun createSocket(host: String?, port: Int, localHost: InetAddress?, localPort: Int): Socket =
        tune(Socket(host, port, localHost, localPort))
    override fun createSocket(host: InetAddress?, port: Int): Socket = tune(Socket(host, port))
    override fun createSocket(address: InetAddress?, port: Int, localAddress: InetAddress?, localPort: Int): Socket =
        tune(Socket(address, port, localAddress, localPort))
}

/** Normalises a host string and builds the nxremote WebSocket URL. */
object HostUrl {
    /**
     * Accepts HOST, HOST:PORT, or a full http(s)/ws(s) URL. Returns a ws(s) URL
     * ending in /v1/ws with the token (and optional release_on_close=false).
     */
    fun build(host: String, token: String = "", releaseOnClose: Boolean = true): String {
        var raw = host.trim()
        require(raw.isNotEmpty()) { "no host given" }
        if (!raw.contains("://")) raw = "ws://$raw"
        val uri = Uri.parse(raw)
        val scheme = when (uri.scheme?.lowercase()) {
            "http", "ws" -> "ws"
            "https", "wss" -> "wss"
            else -> throw IllegalArgumentException("unsupported scheme in '$host'")
        }
        val h = uri.host ?: throw IllegalArgumentException("no host in '$host'")
        val port = if (uri.port != -1) uri.port else 8000
        require(port in 1..65535) { "invalid port in '$host'" }
        val sb = StringBuilder("$scheme://$h:$port/v1/ws")
        val q = mutableListOf<String>()
        if (token.isNotEmpty()) q.add("token=" + Uri.encode(token))
        if (!releaseOnClose) q.add("release_on_close=false")
        if (q.isNotEmpty()) sb.append("?").append(q.joinToString("&"))
        return sb.toString()
    }
}

/**
 * Persistent WebSocket client for an nxremote host. Mirrors the Python client:
 * sends {"type":"input"} frames, parses rumble/state events, reconnects with
 * backoff. All callbacks run on OkHttp's dispatcher thread.
 */
class NxWebSocketClient(
    private val url: String,
    private val onRumble: (RumbleState) -> Unit = {},
    private val onStatus: (String, Boolean) -> Unit = { _, _ -> },
    private val onError: (String) -> Unit = {},
    private val binary: Boolean = true,
) {
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // streaming; no read timeout
        .retryOnConnectionFailure(true)
        // Disable Nagle's algorithm: input frames are tiny and latency-critical,
        // and without TCP_NODELAY the kernel can delay a small packet ~40 ms.
        .socketFactory(NoDelaySocketFactory)
        .build()

    @Volatile private var ws: WebSocket? = null
    @Volatile var connected: Boolean = false
        private set
    private val stopped = AtomicBoolean(false)
    private var backoffMs = 500L
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "nxremote-ws-reconnect").apply { isDaemon = true }
    }

    fun start() {
        stopped.set(false)
        openSocket()
    }

    fun stop() {
        stopped.set(true)
        ws?.close(1000, "client stopping")
        ws = null
        connected = false
        scheduler.shutdownNow()
    }

    /**
     * Send the current controller state. Uses the compact 12-byte binary frame by
     * default (smaller, parse-free on the host); set [binary] = false for JSON.
     * Both are accepted by the host, so this is safe against older servers only if
     * they understand binary — the host here does.
     */
    fun sendState(state: ControllerState) {
        val w = ws ?: return
        if (binary) {
            w.send(okio.ByteString.of(*state.toBinaryFrame()))
        } else {
            w.send(state.normalized().toInputJson())
        }
    }

    fun sendRaw(message: String) {
        ws?.send(message)
    }

    private fun openSocket() {
        if (stopped.get()) return
        val request = try {
            Request.Builder().url(url).build()
        } catch (e: IllegalArgumentException) {
            onStatus("bad host: ${e.message}", false)
            onError(e.message ?: "invalid URL")
            stopped.set(true)
            return
        }
        onStatus("connecting", false)
        ws = client.newWebSocket(request, Listener())
    }

    private fun scheduleReconnect() {
        if (stopped.get()) return
        val delay = backoffMs
        backoffMs = (backoffMs * 2).coerceAtMost(5000L)
        scheduler.schedule({ openSocket() }, delay, TimeUnit.MILLISECONDS)
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            connected = true
            backoffMs = 500L
            onStatus("connected", true)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val obj = JSONObject(text)
                when (obj.optString("type")) {
                    "rumble" -> {
                        val l = obj.optJSONObject("left")?.optDouble("intensity", 0.0) ?: 0.0
                        val r = obj.optJSONObject("right")?.optDouble("intensity", 0.0) ?: 0.0
                        onRumble(RumbleState(l.toFloat(), r.toFloat()))
                    }
                    "hello", "state" -> {
                        val controller = if (obj.has("controller")) obj.getJSONObject("controller") else obj
                        val state = controller.optString("state", "?")
                        val conn = controller.optBoolean("connected", false)
                        onStatus("switch:$state", conn)
                    }
                    "error" -> onError(obj.optString("message"))
                }
            } catch (e: Exception) {
                // Ignore malformed frames.
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            connected = false
            onStatus("reconnecting", false)
            onError(t.message ?: t.toString())
            scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            connected = false
            if (!stopped.get()) {
                onStatus("reconnecting", false)
                scheduleReconnect()
            } else {
                onStatus("stopped", false)
            }
        }
    }
}
