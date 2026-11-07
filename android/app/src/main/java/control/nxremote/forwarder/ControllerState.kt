package control.nxremote.forwarder

import org.json.JSONObject

/**
 * A snapshot of the local controller in the nxremote host's coordinate system.
 *
 * Sticks: x in [-1, 1] (+right), y in [-1, 1] (+up). Button names match the host
 * (see nxremote.hid.state.BUTTONS): A B X Y L R ZL ZR MINUS PLUS HOME CAPTURE
 * L_STICK R_STICK DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT.
 */
data class ControllerState(
    val buttons: Set<String> = emptySet(),
    val lx: Float = 0f,
    val ly: Float = 0f,
    val rx: Float = 0f,
    val ry: Float = 0f,
) {
    private fun clamp(v: Float) = if (v < -1f) -1f else if (v > 1f) 1f else v

    fun normalized(): ControllerState =
        ControllerState(buttons, clamp(lx), clamp(ly), clamp(rx), clamp(ry))

    /** The {"type":"input", ...} message the host's WebSocket expects. */
    fun toInputJson(): String {
        val n = normalized()
        val obj = JSONObject()
        obj.put("type", "input")
        obj.put("buttons", org.json.JSONArray(n.buttons.sorted()))
        obj.put("left_stick", JSONObject().put("x", round4(n.lx)).put("y", round4(n.ly)))
        obj.put("right_stick", JSONObject().put("x", round4(n.rx)).put("y", round4(n.ry)))
        return obj.toString()
    }

    companion object {
        val NEUTRAL = ControllerState()
        private fun round4(v: Float): Double = Math.round(v * 10000.0) / 10000.0
    }
}

/** Latest rumble intensities from the host, 0..1 per actuator. */
data class RumbleState(val left: Float = 0f, val right: Float = 0f) {
    val active: Boolean get() = left > 0f || right > 0f
}
