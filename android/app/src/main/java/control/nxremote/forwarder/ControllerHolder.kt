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
        merged.set(ControllerState(buttons, a.lx, a.ly, a.rx, a.ry))
    }

    /** The current combined controller state for the send loop. */
    fun snapshot(): ControllerState = merged.get()
}
