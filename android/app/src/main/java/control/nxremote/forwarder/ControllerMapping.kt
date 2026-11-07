package control.nxremote.forwarder

import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent

/**
 * Maps Android game-controller input to Switch button names.
 *
 * Android reports face buttons as KEYCODE_BUTTON_A/B/X/Y in the Xbox physical
 * layout (A = bottom, B = right, X = left, Y = top). On a Switch the same
 * physical positions carry different labels. Two conventions:
 *
 *  - positional (default): keep the physical position — Android A (bottom) ->
 *    Switch B. This is what a Switch player expects (the bottom button confirms).
 *  - labels: match the printed letters — Android A -> Switch A.
 */
class ControllerMapping(
    val positional: Boolean = true,
    val triggerThreshold: Float = 0.4f,
    val deadzone: Float = 0.08f,
) {
    private val faceButton: Map<Int, String> = if (positional) {
        mapOf(
            KeyEvent.KEYCODE_BUTTON_A to "B",
            KeyEvent.KEYCODE_BUTTON_B to "A",
            KeyEvent.KEYCODE_BUTTON_X to "Y",
            KeyEvent.KEYCODE_BUTTON_Y to "X",
        )
    } else {
        mapOf(
            KeyEvent.KEYCODE_BUTTON_A to "A",
            KeyEvent.KEYCODE_BUTTON_B to "B",
            KeyEvent.KEYCODE_BUTTON_X to "X",
            KeyEvent.KEYCODE_BUTTON_Y to "Y",
        )
    }

    private val keyToButton: Map<Int, String> = faceButton + mapOf(
        KeyEvent.KEYCODE_BUTTON_L1 to "L",
        KeyEvent.KEYCODE_BUTTON_R1 to "R",
        KeyEvent.KEYCODE_BUTTON_L2 to "ZL",   // digital triggers, if the pad reports them as keys
        KeyEvent.KEYCODE_BUTTON_R2 to "ZR",
        KeyEvent.KEYCODE_BUTTON_THUMBL to "L_STICK",
        KeyEvent.KEYCODE_BUTTON_THUMBR to "R_STICK",
        KeyEvent.KEYCODE_BUTTON_SELECT to "MINUS",
        KeyEvent.KEYCODE_BUTTON_START to "PLUS",
        KeyEvent.KEYCODE_BUTTON_MODE to "HOME",
        // D-pad when reported as keys (some pads) rather than HAT axes.
        KeyEvent.KEYCODE_DPAD_UP to "DPAD_UP",
        KeyEvent.KEYCODE_DPAD_DOWN to "DPAD_DOWN",
        KeyEvent.KEYCODE_DPAD_LEFT to "DPAD_LEFT",
        KeyEvent.KEYCODE_DPAD_RIGHT to "DPAD_RIGHT",
    )

    /** The Switch button for a key event, or null if it isn't a mapped gamepad key. */
    fun button(keyCode: Int): String? = keyToButton[keyCode]

    /** True if this key code is one we handle (so the Activity can consume it). */
    fun handles(keyCode: Int): Boolean =
        keyToButton.containsKey(keyCode) || KeyEvent.isGamepadButton(keyCode)

    private fun dz(v: Float): Float = if (kotlin.math.abs(v) < deadzone) 0f else v

    /**
     * Derive sticks, analog triggers (-> ZL/ZR) and the D-pad HAT from a MotionEvent.
     * Returns the stick values plus the set of buttons contributed by axes.
     */
    fun fromMotion(event: MotionEvent): AxisResult {
        val dev = event.device
        fun axis(a: Int): Float = centeredAxis(event, dev, a)

        val lx = dz(axis(MotionEvent.AXIS_X))
        val ly = dz(-axis(MotionEvent.AXIS_Y))   // Android Y grows down; host +y is up
        // Right stick is Z/RZ on most Android pads.
        val rx = dz(axis(MotionEvent.AXIS_Z))
        val ry = dz(-axis(MotionEvent.AXIS_RZ))

        val buttons = mutableSetOf<String>()
        val lt = maxOf(axis(MotionEvent.AXIS_LTRIGGER), axis(MotionEvent.AXIS_BRAKE))
        val rt = maxOf(axis(MotionEvent.AXIS_RTRIGGER), axis(MotionEvent.AXIS_GAS))
        if (lt >= triggerThreshold) buttons.add("ZL")
        if (rt >= triggerThreshold) buttons.add("ZR")

        val hatX = axis(MotionEvent.AXIS_HAT_X)
        val hatY = axis(MotionEvent.AXIS_HAT_Y)
        if (hatX < -0.5f) buttons.add("DPAD_LEFT")
        if (hatX > 0.5f) buttons.add("DPAD_RIGHT")
        if (hatY < -0.5f) buttons.add("DPAD_UP")
        if (hatY > 0.5f) buttons.add("DPAD_DOWN")

        return AxisResult(lx, ly, rx, ry, buttons)
    }

    private fun centeredAxis(event: MotionEvent, device: InputDevice?, axis: Int): Float {
        val range = device?.getMotionRange(axis, event.source)
        val value = event.getAxisValue(axis)
        if (range != null) {
            val flat = range.flat
            if (kotlin.math.abs(value) <= flat) return 0f
        }
        return value
    }

    data class AxisResult(
        val lx: Float,
        val ly: Float,
        val rx: Float,
        val ry: Float,
        val buttons: Set<String>,
    )

    companion object {
        /** A source is a real game controller (gamepad/joystick), not the touch screen. */
        fun isGameController(source: Int): Boolean {
            val joystick = source and InputDevice.SOURCE_JOYSTICK == InputDevice.SOURCE_JOYSTICK
            val gamepad = source and InputDevice.SOURCE_GAMEPAD == InputDevice.SOURCE_GAMEPAD
            return joystick || gamepad
        }
    }
}
