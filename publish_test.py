"""Simulate ESP32 registration, telemetry, and device-detected alerts."""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("TEST_MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("TEST_MQTT_PASSWORD") or None

DEVICE_ID = os.getenv("DEVICE_ID", "tracker-001")

if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", DEVICE_ID):
    raise ValueError(
        "DEVICE_ID must contain only letters, numbers, underscores "
        "or hyphens and be at most 64 characters"
    )

MESSAGE_TOPIC = f"coldchain/{DEVICE_ID}/messages"
ACK_TOPIC = f"coldchain/{DEVICE_ID}/registration/ack"
CONFIG_TOPIC = f"coldchain/{DEVICE_ID}/config"

BOOT_ID = uuid.uuid4().hex[:8]

INITIAL_DEVICE_SETTINGS = {
    "max_temperature_c": 8.0,
    "shock_threshold_g": 4.0,
}

message_seq = -1
subscriptions_ready = threading.Event()
registration_ack_received = threading.Event()

registration_response = {}
configuration_update = {}
callback_error = {}


def timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_message(message_type: str, payload: dict, uptime_ms: int) -> dict:
    global message_seq
    message_seq += 1

    return {
        "schema_version": 1,
        "message_type": message_type,
        "device_id": DEVICE_ID,
        "firmware_version": "0.1.0-test",
        "boot_id": BOOT_ID,
        "message_seq": message_seq,
        "observed_at": timestamp(),
        "uptime_ms": uptime_ms,
        "payload": payload,
    }


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        callback_error["error"] = f"MQTT connection failed: {reason_code}"
        subscriptions_ready.set()
        return

    # Subscribe before publishing registration so the ACK isn't missed.
    client.subscribe(
        [
            (ACK_TOPIC, 1),
            (CONFIG_TOPIC, 1),
        ]
    )


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    if any(code.is_failure for code in reason_code_list):
        callback_error["error"] = "Broker rejected an MQTT subscription"

    subscriptions_ready.set()


def on_message(client, userdata, message):
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(f"Ignoring invalid JSON on {message.topic}")
        return

    if message.topic == ACK_TOPIC:
        registration_response.clear()
        registration_response.update(payload)
        registration_ack_received.set()

    elif message.topic == CONFIG_TOPIC:
        configuration_update.clear()
        configuration_update.update(payload)
        print(f"Received retained device configuration: {payload}")


def publish(client, message):
    info = client.publish(
        MESSAGE_TOPIC,
        json.dumps(message),
        qos=1,
    )
    info.wait_for_publish(timeout=10)

    if not info.is_published():
        raise RuntimeError(
            f"Timed out publishing {message['message_type']}"
        )

    print(
        f"Published {message['message_type']} "
        f"(seq={message['message_seq']}) to {MESSAGE_TOPIC}"
    )

    # Make the local demonstration easier to follow.
    time.sleep(0.3)


def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"coldchain-test-{uuid.uuid4().hex[:8]}",
    )

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    try:
        if not subscriptions_ready.wait(timeout=10):
            raise RuntimeError("Timed out subscribing to registration ACK")

        if "error" in callback_error:
            raise RuntimeError(callback_error["error"])

        # 1. Register with the settings provisioned on this test device.
        registration = make_message(
            "register",
            {"settings": INITIAL_DEVICE_SETTINGS},
            uptime_ms=0,
        )
        publish(client, registration)

        # 2. Wait for the backend to return the canonical stored settings.
        if not registration_ack_received.wait(timeout=10):
            raise RuntimeError(
                "No registration acknowledgement received. "
                "Check the backend, broker, and MQTT ACLs."
            )

        if registration_response.get("status") == "rejected":
            raise RuntimeError(
                "Registration rejected: "
                + registration_response.get("reason", "unknown reason")
            )

        settings = registration_response.get("settings")
        if not isinstance(settings, dict):
            raise RuntimeError(
                "Registration acknowledgement did not include settings"
            )

        max_temperature_c = float(settings["max_temperature_c"])
        shock_threshold_g = float(settings["shock_threshold_g"])

        print(f"Registration status: {registration_response['status']}")
        print(f"Stored temperature limit: {max_temperature_c} C")
        print(f"Stored shock threshold: {shock_threshold_g} g")

        # 3. Publish ordinary telemetry.
        publish(
            client,
            make_message(
                "telemetry",
                {
                    "temperature": {
                        "valid": True,
                        "celsius": 5.4,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 1.08,
                        "shock_count_since_last_report": 0,
                    },
                    "location": {
                        "fix_valid": False,
                        "latitude": None,
                        "longitude": None,
                        "accuracy_m": None,
                        "satellites_used": 0,
                    },
                },
                uptime_ms=15000,
            ),
        )

        # 4. Simulate sensor readings. Real firmware reads its physical sensors.
        temperature_c = max_temperature_c + 1.1
        peak_acceleration_g = shock_threshold_g + 1.7

        # 5. These comparisons simulate local ESP32 logic.
        publish(
            client,
            make_message(
                "telemetry",
                {
                    "temperature": {
                        "valid": True,
                        "celsius": temperature_c,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": (
                            peak_acceleration_g
                        ),
                        "shock_count_since_last_report": 1,
                    },
                    "location": {
                        "fix_valid": False,
                        "latitude": None,
                        "longitude": None,
                        "accuracy_m": None,
                        "satellites_used": 0,
                    },
                },
                uptime_ms=30000,
            ),
        )

        # Simulated firmware detects a temperature breach and reports it.
        if temperature_c > max_temperature_c:
            publish(
                client,
                make_message(
                    "event",
                    {
                        "type": "temperature_breach",
                        "temperature_c": temperature_c,
                        "maximum_allowed_c": max_temperature_c,
                    },
                    uptime_ms=30100,
                ),
            )

        # Simulated firmware detects a shock and reports it.
        if peak_acceleration_g > shock_threshold_g:
            publish(
                client,
                make_message(
                    "event",
                    {
                        "type": "shock_detected",
                        "peak_acceleration_magnitude_g": (
                            peak_acceleration_g
                        ),
                        "threshold_g": shock_threshold_g,
                        "duration_ms": 35,
                    },
                    uptime_ms=30200,
                ),
            )

        print(f"Test completed for {DEVICE_ID}")

    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
