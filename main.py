import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import paho.mqtt.client as mqtt
import psycopg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("coldchain")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/coldchain",
)
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MAX_TEMPERATURE_C = float(os.getenv("MAX_TEMPERATURE_C", "8.0"))
ONLINE_TIMEOUT_SECONDS = int(os.getenv("ONLINE_TIMEOUT_SECONDS", "60"))


class DeviceMessage(BaseModel):
    """Common envelope sent by the ESP32, with a type-specific payload."""

    schema_version: int
    message_type: Literal["telemetry", "event"]
    device_id: str
    firmware_version: str
    boot_id: str
    message_seq: int
    observed_at: datetime
    uptime_ms: int
    payload: dict[str, Any]


def connect_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    """Create the minimal tables automatically on application startup."""
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                firmware_version TEXT NOT NULL,
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                latest_observed_at TIMESTAMPTZ NOT NULL,
                latest_message JSONB NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id BIGSERIAL PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                observed_at TIMESTAMPTZ NOT NULL,
                boot_id TEXT NOT NULL,
                message_seq BIGINT NOT NULL,
                data JSONB NOT NULL,
                UNIQUE (device_id, boot_id, message_seq)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                observed_at TIMESTAMPTZ NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                acknowledged_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS telemetry_device_time_idx "
            "ON telemetry (device_id, observed_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS alerts_device_status_idx "
            "ON alerts (device_id, status, observed_at DESC)"
        )


def insert_alert(
    conn,
    *,
    device_id: str,
    alert_type: str,
    severity: str,
    observed_at: datetime,
    details: dict[str, Any],
) -> None:
    """Insert an alert. Avoid repeated open temperature alerts for one device."""
    if alert_type == "temperature_breach":
        existing = conn.execute(
            "SELECT 1 FROM alerts WHERE device_id = %s AND type = %s AND status = 'open' LIMIT 1",
            (device_id, alert_type),
        ).fetchone()
        if existing:
            return

    alert_id = f"alert-{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO alerts (id, device_id, type, severity, status, observed_at, details)
        VALUES (%s, %s, %s, %s, 'open', %s, %s)
        """,
        (alert_id, device_id, alert_type, severity, observed_at, Jsonb(details)),
    )
    logger.warning("Created %s alert %s for %s", alert_type, alert_id, device_id)


def handle_mqtt_message(client, userdata, mqtt_message) -> None:
    """Validate an MQTT message, persist it, and create alerts when needed."""
    try:
        message = DeviceMessage.model_validate_json(mqtt_message.payload)
        data = message.model_dump(mode="json")

        with connect_db() as conn:
            # Keep a registry of devices that have sent at least one valid message.
            # last_seen uses server receive time; latest_message uses device event time.
            conn.execute(
                """
                INSERT INTO devices
                    (device_id, firmware_version, last_seen, latest_observed_at, latest_message)
                VALUES (%s, %s, now(), %s, %s)
                ON CONFLICT (device_id) DO UPDATE SET
                    firmware_version = EXCLUDED.firmware_version,
                    last_seen = now(),
                    latest_observed_at = GREATEST(devices.latest_observed_at, EXCLUDED.latest_observed_at),
                    latest_message = CASE
                        WHEN EXCLUDED.latest_observed_at >= devices.latest_observed_at
                        THEN EXCLUDED.latest_message
                        ELSE devices.latest_message
                    END
                """,
                (message.device_id, message.firmware_version, message.observed_at, Jsonb(data)),
            )

            if message.message_type == "telemetry":
                # Store full envelopes, so clients retain firmware, sequence, timestamp and payload.
                conn.execute(
                    """
                    INSERT INTO telemetry (device_id, observed_at, boot_id, message_seq, data)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (device_id, boot_id, message_seq) DO NOTHING
                    """,
                    (
                        message.device_id,
                        message.observed_at,
                        message.boot_id,
                        message.message_seq,
                        Jsonb(data),
                    ),
                )

                temperature = message.payload.get("temperature", {})
                celsius = temperature.get("celsius")
                if (
                    temperature.get("valid") is True
                    and isinstance(celsius, (int, float))
                    and celsius > MAX_TEMPERATURE_C
                ):
                    insert_alert(
                        conn,
                        device_id=message.device_id,
                        alert_type="temperature_breach",
                        severity="warning",
                        observed_at=message.observed_at,
                        details={
                            "temperature_c": celsius,
                            "maximum_allowed_c": MAX_TEMPERATURE_C,
                        },
                    )

            elif message.message_type == "event":
                event_type = message.payload.get("type")
                if event_type in {"shock_detected", "temperature_breach"}:
                    details = {
                        key: value
                        for key, value in message.payload.items()
                        if key != "type"
                    }
                    insert_alert(
                        conn,
                        device_id=message.device_id,
                        alert_type=event_type,
                        severity="warning",
                        observed_at=message.observed_at,
                        details=details,
                    )

    except (ValidationError, ValueError) as exc:
        logger.warning("Ignoring invalid MQTT message on %s: %s", mqtt_message.topic, exc)
    except Exception:
        # Do not let a malformed message or transient database error crash the MQTT loop.
        logger.exception("Could not process MQTT message on %s", mqtt_message.topic)


def handle_mqtt_connect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code.is_failure:
        logger.error("MQTT connection failed: %s", reason_code)
        return
    logger.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
    client.subscribe(MQTT_TOPIC, qos=1)
    logger.info("Subscribed to MQTT topic %s", MQTT_TOPIC)


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="coldchain-backend",
)
mqtt_client.on_connect = handle_mqtt_connect
mqtt_client.on_message = handle_mqtt_message


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # connect_async lets the HTTP API start even if the broker is temporarily unavailable.
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    try:
        yield
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()


app = FastAPI(
    title="Cold-chain Device API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/v1/devices")
def list_devices():
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT device_id, last_seen FROM devices ORDER BY device_id"
        ).fetchall()

    now = datetime.now(timezone.utc)
    items = []
    for row in rows:
        age_seconds = (now - row["last_seen"]).total_seconds()
        items.append(
            {
                "device_id": row["device_id"],
                "status": "online" if age_seconds <= ONLINE_TIMEOUT_SECONDS else "offline",
            }
        )
    return {"items": items}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str):
    """Return the most recent complete JSON message received from this device."""
    with connect_db() as conn:
        row = conn.execute(
            "SELECT latest_message FROM devices WHERE device_id = %s",
            (device_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return row["latest_message"]


@app.get("/api/v1/devices/{device_id}/telemetry")
def get_telemetry(
    device_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT data FROM telemetry
            WHERE device_id = %s
            ORDER BY observed_at DESC, id DESC
            LIMIT %s
            """,
            (device_id, limit),
        ).fetchall()
    return {"items": [row["data"] for row in rows]}


@app.get("/api/v1/alerts")
def get_alerts(
    status: str | None = None,
    device_id: str | None = None,
):
    conditions: list[str] = []
    params: list[Any] = []
    if status is not None:
        conditions.append("status = %s")
        params.append(status)
    if device_id is not None:
        conditions.append("device_id = %s")
        params.append(device_id)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = (
        "SELECT id, device_id, type, severity, status, observed_at, details "
        "FROM alerts" + where_clause + " ORDER BY observed_at DESC"
    )
    with connect_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"items": rows}


@app.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            UPDATE alerts
            SET status = 'acknowledged',
                acknowledged_at = COALESCE(acknowledged_at, now())
            WHERE id = %s
            RETURNING id, status, acknowledged_at
            """,
            (alert_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {
        "alert_id": row["id"],
        "status": row["status"],
        "acknowledged_at": row["acknowledged_at"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
