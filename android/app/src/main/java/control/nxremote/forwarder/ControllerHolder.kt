package control.nxremote.forwarder

import java.util.concurrent.atomic.AtomicReference

/**
 * Process-wide bridge between the Activity (which receives OS input events) and
 * the ForwarderService (which owns the socket and the send loop).
 *
 * The Activity merges key presses with the latest axis snapshot and publishes a
 * combined [ControllerState]; the service reads it at its send rate. The service
 * publishes the latest [RumbleState] for the Activity to play on the pad.
 */
object ControllerHolder {
    private val heldButtons = HashSet<String>()           // from key down/up
    @Volatile private var axes = ControllerMapping.AxisResult(0f, 0f, 0f, 0f, emptySet())
    private val merged = AtomicReference(ControllerState.NEUTRAL)

    val rumble = AtomicReference(RumbleState())
    @Volatile var lastDeviceName: String = "none"

    // Monotonic counter bumped whenever the merged state actually changes, with a
    // lock to let the send loop block until the next change (instead of polling on
    // a fixed timer, which adds up to a full poll period of input latency).
    private val revLock = Object()
    @Volatile private var revision = 0L

    @Synchronized
    fun setButton(name: String, pressed: Boolean) {
        if (pressed) heldButtons.add(name) else heldButtons.remove(name)
        recompute()
    }

    @Synchronized
    fun setAxes(result: ControllerMapping.AxisResult) {
        axes = result
        recompute()
    }

    @Synchronized
    fun releaseAll() {
        heldButtons.clear()
        axes = ControllerMapping.AxisResult(0f, 0f, 0f, 0f, emptySet())
        recompute()
    }

    private fun recompute() {
        val a = axes
        val buttons = HashSet(heldButtons)
        buttons.addAll(a.buttons)   // trigger + hat contributions
        val next = ControllerState(buttons, a.lx, a.ly, a.rx, a.ry)
        if (next != merged.get()) {
            merged.set(next)
            synchronized(revLock) {
                revision++
                revLock.notifyAll()
            }
        }
    }

    /** The current combined controller state for the send loop. */
    fun snapshot(): ControllerState = merged.get()

    fun revision(): Long = revision

    /**
     * Block until the merged state changes past [since], or until [timeoutMs]
     * elapses. Returns the current revision. Lets the send loop fire the instant
     * an input arrives while still bounding the idle keep-alive rate.
     */
    fun awaitChange(since: Long, timeoutMs: Long): Long {
        synchronized(revLock) {
            if (revision == since && timeoutMs > 0) {
                try {
                    revLock.wait(timeoutMs)
                } catch (_: InterruptedException) {
                }
            }
            return revision
        }
    }
}
