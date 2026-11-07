package control.nxremote.forwarder

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Minimal control panel + the input capture point.
 *
 * The Activity receives the physical controller's key and motion events from the
 * OS (when focused) and publishes them to [ControllerHolder]; the
 * [ForwarderService] reads that and streams it to the host, and keeps running
 * when this Activity is backgrounded. Rumble is played by the service.
 */
class MainActivity : AppCompatActivity() {

    private val mapping = ControllerMapping()
    private lateinit var hostField: EditText
    private lateinit var tokenField: EditText
    private lateinit var statusView: TextView
    private lateinit var inputView: TextView
    private lateinit var toggleButton: Button
    private lateinit var prefs: android.content.SharedPreferences

    private val uiHandler by lazy { android.os.Handler(mainLooper) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("nxremote", Context.MODE_PRIVATE)
        setContentView(buildUi())
        refreshToggle()
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        ForwarderService.statusListener = { s, c -> uiHandler.post { showStatus(s, c) } }
        showStatus(ForwarderService.status, ForwarderService.connected)
        startInputTicker()
    }

    override fun onPause() {
        super.onPause()
        ForwarderService.statusListener = null
    }

    // -- input capture -----------------------------------------------------------

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (ControllerMapping.isGameController(event.source) || mapping.handles(event.keyCode)) {
            val name = mapping.button(event.keyCode)
            if (name != null) {
                when (event.action) {
                    KeyEvent.ACTION_DOWN -> ControllerHolder.setButton(name, true)
                    KeyEvent.ACTION_UP -> ControllerHolder.setButton(name, false)
                }
                // Don't consume START/MODE so the system can still act if needed; consume the rest.
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onGenericMotionEvent(event: MotionEvent): Boolean {
        if (event.action == MotionEvent.ACTION_MOVE &&
            ControllerMapping.isGameController(event.source)
        ) {
            ControllerHolder.setAxes(mapping.fromMotion(event))
            return true
        }
        return super.onGenericMotionEvent(event)
    }

    private fun startInputTicker() {
        // Reflect the live merged state in the UI so you can see the pad is read.
        val tick = object : Runnable {
            override fun run() {
                val s = ControllerHolder.snapshot()
                val pads = InputDevice.getDeviceIds().toList()
                    .mapNotNull { InputDevice.getDevice(it) }
                    .count { ControllerMapping.isGameController(it.sources) }
                inputView.text = buildString {
                    append("controllers: ").append(pads).append('\n')
                    append("buttons: ").append(s.buttons.sorted().joinToString(" ").ifEmpty { "—" }).append('\n')
                    append("L(%.2f, %.2f)  R(%.2f, %.2f)".format(s.lx, s.ly, s.rx, s.ry))
                }
                uiHandler.postDelayed(this, 100)
            }
        }
        uiHandler.post(tick)
    }

    // -- service control ---------------------------------------------------------

    private fun toggle() {
        if (ForwarderService.running) {
            startService(Intent(this, ForwarderService::class.java).setAction(ForwarderService.ACTION_STOP))
        } else {
            val host = hostField.text.toString().trim()
            if (host.isEmpty()) { showStatus("enter a host, e.g. 192.168.1.50:8000", false); return }
            prefs.edit().putString("host", host).putString("token", tokenField.text.toString()).apply()
            val intent = Intent(this, ForwarderService::class.java)
                .setAction(ForwarderService.ACTION_START)
                .putExtra(ForwarderService.EXTRA_HOST, host)
                .putExtra(ForwarderService.EXTRA_TOKEN, tokenField.text.toString().trim())
            ContextCompat.startForegroundService(this, intent)
        }
        uiHandler.postDelayed({ refreshToggle() }, 150)
    }

    private fun refreshToggle() {
        toggleButton.text = if (ForwarderService.running) "Stop forwarding" else "Start forwarding"
    }

    private fun showStatus(s: String, connected: Boolean) {
        statusView.text = "status: $s"
        statusView.setTextColor(if (connected) 0xFF2ECC71.toInt() else 0xFFBBBBBB.toInt())
        refreshToggle()
    }

    // -- UI (built in code; no XML needed) ---------------------------------------

    private fun buildUi(): View {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        fun label(t: String) = TextView(this).apply { text = t; setPadding(0, pad / 2, 0, pad / 4) }

        root.addView(TextView(this).apply {
            text = "nxremote controller forwarder"
            textSize = 20f
            setPadding(0, 0, 0, pad / 2)
        })

        root.addView(label("Host (IP[:port] or ws(s)://…)"))
        hostField = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setText(prefs.getString("host", ""))
            hint = "192.168.1.50:8000"
        }
        root.addView(hostField)

        root.addView(label("API token (blank if host uses --no-auth)"))
        tokenField = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_PASSWORD or InputType.TYPE_CLASS_TEXT
            setText(prefs.getString("token", ""))
        }
        root.addView(tokenField)

        toggleButton = Button(this).apply {
            text = "Start forwarding"
            setOnClickListener { toggle() }
        }
        root.addView(toggleButton, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = pad })

        statusView = TextView(this).apply { text = "status: stopped"; setPadding(0, pad, 0, 0) }
        root.addView(statusView)

        inputView = TextView(this).apply {
            text = "controllers: 0"
            typeface = android.graphics.Typeface.MONOSPACE
            setPadding(0, pad, 0, 0)
        }
        root.addView(inputView)

        root.addView(TextView(this).apply {
            text = "Pair or plug in a controller, enter your host, and tap Start. " +
                "Forwarding keeps running in the background via a persistent notification. " +
                "Keep this screen focused to capture the controller; the service holds the connection."
            setPadding(0, pad, 0, 0)
            gravity = Gravity.START
            alpha = 0.7f
        })
        return root
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }
}
