"""Send sample telemetry and shock messages to the local MQTT broker."""
import json
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

host = os.getenv("MQTT_HOST", "127.0.0.1")
port = int(os.getenv("MQTT_PORT", "1883"))
device_id = os.getenv("DEVICE_ID", "tracker-001")
topic = f"coldchain/{device_id}/messages"
now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="coldchain-test-publisher")
client.connect(host, port, keepalive=60)
client.loop_start()

telemetry = {
    "schema_version": 1,
    "message_type": "telemetry",
    "device_id": device_id,
    "firmware_version": "0.1.0",
    "boot_id": "demo-boot-001",
    "message_seq": 42,
    "observed_at": now,
    "uptime_ms": 120000,
    "payload": {
        "temperature": {"valid": True, "celsius": 9.1},
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
}

shock = {
    "schema_version": 1,
    "message_type": "event",
    "device_id": device_id,
    "firmware_version": "0.1.0",
    "boot_id": "demo-boot-001",
    "message_seq": 43,
    "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "uptime_ms": 121000,
    "payload": {
        "type": "shock_detected",
        "peak_acceleration_magnitude_g": 5.7,
        "threshold_g": 4.0,
        "duration_ms": 35,
    },
}

try:
    for message in (telemetry, shock):
        info = client.publish(topic, json.dumps(message), qos=1)
        info.wait_for_publish()
        print(f"Published {message['message_type']} to {topic}")
        time.sleep(0.2)
finally:
    client.disconnect()
    client.loop_stop()
