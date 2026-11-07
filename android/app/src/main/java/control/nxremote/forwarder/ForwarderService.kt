package control.nxremote.forwarder

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.CombinedVibration
import android.os.IBinder
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Keeps forwarding the controller while the app is backgrounded or the screen is
 * off. A foreground service with an ongoing notification is the Android-sanctioned
 * way to do long-running work the user started; combined with a partial wake lock
 * the send loop keeps running with the screen off.
 */
class ForwarderService : Service() {

    companion object {
        const val ACTION_START = "control.nxremote.forwarder.START"
        const val ACTION_STOP = "control.nxremote.forwarder.STOP"
        const val EXTRA_HOST = "host"
        const val EXTRA_TOKEN = "token"
        private const val CHANNEL_ID = "forwarding"
        private const val NOTIF_ID = 1
        private const val SEND_HZ = 120L
        private const val KEEPALIVE_MS = 1000L

        @Volatile var running: Boolean = false
            private set
        @Volatile var status: String = "stopped"
            private set
        @Volatile var connected: Boolean = false
            private set
        // Set by the service so the UI can observe without binding.
        @Volatile var statusListener: ((String, Boolean) -> Unit)? = null
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var sendJob: Job? = null
    private var client: NxWebSocketClient? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val vibrator: Vibrator? by lazy { obtainVibrator() }
    @Volatile private var lastRumbleActive = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopForwarding(); return START_NOT_STICKY }
            else -> startForwarding(intent)
        }
        return START_STICKY
    }

    private fun startForwarding(intent: Intent?) {
        val host = intent?.getStringExtra(EXTRA_HOST)?.trim().orEmpty()
        val token = intent?.getStringExtra(EXTRA_TOKEN)?.trim().orEmpty()
        if (host.isEmpty()) { stopSelf(); return }

        createChannel()
        startForegroundCompat(buildNotification("connecting…"))
        acquireWakeLock()

        val url = try {
            HostUrl.build(host, token, releaseOnClose = true)
        } catch (e: IllegalArgumentException) {
            updateStatus("bad host: ${e.message}", false)
            stopForwarding()
            return
        }

        client = NxWebSocketClient(
            url = url,
            onRumble = { playRumble(it) },
            onStatus = { s, c -> updateStatus(s, c) },
            onError = { /* surfaced through status */ },
        ).also { it.start() }

        running = true
        sendJob?.cancel()
        sendJob = scope.launch { sendLoop() }
    }

    private suspend fun sendLoop() {
        val period = 1000L / SEND_HZ
        var lastSent: ControllerState? = null
        var lastSentAt = 0L
        while (currentCoroutineContext().isActive) {
            val state = ControllerHolder.snapshot()
            val now = System.currentTimeMillis()
            val c = client
            if (c != null && c.connected) {
                if (state != lastSent || now - lastSentAt >= KEEPALIVE_MS) {
                    c.sendState(state)
                    lastSent = state
                    lastSentAt = now
                }
            } else {
                lastSent = null
            }
            delay(period)
        }
    }

    private fun stopForwarding() {
        running = false
        connected = false
        status = "stopped"
        sendJob?.cancel()
        sendJob = null
        client?.stop()
        client = null
        stopRumble()
        releaseWakeLock()
        statusListener?.invoke("stopped", false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun updateStatus(s: String, c: Boolean) {
        status = s
        connected = c
        statusListener?.invoke(s, c)
        // Refresh the ongoing notification text.
        val mgr = getSystemService(NotificationManager::class.java)
        mgr?.notify(NOTIF_ID, buildNotification(s))
    }

    // -- rumble playback ---------------------------------------------------------

    private fun playRumble(r: RumbleState) {
        val v = vibrator ?: return
        if (!r.active) {
            stopRumble()
            lastRumbleActive = false
            return
        }
        val amplitude = (maxOf(r.left, r.right).coerceIn(0f, 1f) * 255).toInt().coerceIn(1, 255)
        try {
            // Repeat a short pulse so sustained rumble doesn't gap between events.
            val effect = VibrationEffect.createOneShot(250, amplitude)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                v.vibrate(effect)
            } else {
                @Suppress("DEPRECATION")
                v.vibrate(longArrayOf(0, 250), -1)
            }
            lastRumbleActive = true
        } catch (_: Exception) {
        }
    }

    private fun stopRumble() {
        if (lastRumbleActive) {
            try { vibrator?.cancel() } catch (_: Exception) {}
        }
    }

    private fun obtainVibrator(): Vibrator? = try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val mgr = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
            mgr?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
    } catch (_: Exception) {
        null
    }

    // -- wake lock ---------------------------------------------------------------

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "nxremote:forwarder").apply {
            setReferenceCounted(false)
            acquire(12 * 60 * 60 * 1000L) // cap at 12h as a safety net
        }
    }

    private fun releaseWakeLock() {
        try { if (wakeLock?.isHeld == true) wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
    }

    // -- notification ------------------------------------------------------------

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Controller forwarding", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "Keeps the controller connected to your nxremote host" }
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, ForwarderService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("nxremote — forwarding")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun startForegroundCompat(notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    override fun onDestroy() {
        scope.cancel()
        releaseWakeLock()
        running = false
        super.onDestroy()
    }
}
