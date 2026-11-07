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

        /** Button order for the compact binary frame; must match the host's BUTTON_NAMES. */
        val BINARY_BUTTON_ORDER = listOf(
            "Y", "X", "B", "A", "R", "ZR",
            "MINUS", "PLUS", "R_STICK", "L_STICK", "HOME", "CAPTURE",
            "DPAD_DOWN", "DPAD_UP", "DPAD_RIGHT", "DPAD_LEFT", "L", "ZL",
        )
        const val BINARY_MAGIC = 0xB1
    }

    /**
     * Encode as the host's 12-byte binary input frame (little-endian):
     * magic, 3-byte button bitmask (BINARY_BUTTON_ORDER), 4x int16 axes.
     * Smaller than JSON and parse-free on the host.
     */
    fun toBinaryFrame(): ByteArray {
        val n = normalized()
        var mask = 0
        for (i in BINARY_BUTTON_ORDER.indices) {
            if (BINARY_BUTTON_ORDER[i] in n.buttons) mask = mask or (1 shl i)
        }
        fun i16(v: Float): Int = Math.round(v * 32767f)
        val buf = java.nio.ByteBuffer.allocate(12).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        buf.put(BINARY_MAGIC.toByte())
        buf.put((mask and 0xFF).toByte())
        buf.put(((mask shr 8) and 0xFF).toByte())
        buf.put(((mask shr 16) and 0xFF).toByte())
        buf.putShort(i16(n.lx).toShort())
        buf.putShort(i16(n.ly).toShort())
        buf.putShort(i16(n.rx).toShort())
        buf.putShort(i16(n.ry).toShort())
        return buf.array()
    }
}

/** Latest rumble intensities from the host, 0..1 per actuator. */
data class RumbleState(val left: Float = 0f, val right: Float = 0f) {
    val active: Boolean get() = left > 0f || right > 0f
}
