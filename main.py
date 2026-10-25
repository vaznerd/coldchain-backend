import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import paho.mqtt.client as mqtt
import psycopg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError, field_validator
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("coldchain")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://coldchain:dev_only_change_me@127.0.0.1:5432/coldchain",
)

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

ONLINE_TIMEOUT_SECONDS = 60
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------


class DeviceMessage(BaseModel):
    """Common envelope published by an ESP32."""

    schema_version: Literal[1]
    message_type: Literal["register", "telemetry", "event"]
    device_id: str
    firmware_version: str
    boot_id: str
    message_seq: int = Field(ge=0)
    observed_at: datetime
    uptime_ms: int = Field(ge=0)
    payload: dict[str, Any]

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value


class DeviceSettings(BaseModel):
    """Thresholds supplied by a device during registration."""

    max_temperature_c: float = Field(
        ge=-100,
        le=100,
        allow_inf_nan=False,
    )
    shock_threshold_g: float = Field(
        gt=0,
        le=1000,
        allow_inf_nan=False,
    )

    @field_validator(
        "max_temperature_c",
        "shock_threshold_g",
        mode="before",
    )
    @classmethod
    def reject_boolean_thresholds(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid thresholds")
        return value


class DeviceSettingsUpdate(BaseModel):
    """API request for changing one or both device settings."""

    max_temperature_c: float | None = Field(
        default=None,
        ge=-100,
        le=100,
        allow_inf_nan=False,
    )
    shock_threshold_g: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        allow_inf_nan=False,
    )

    @field_validator(
        "max_temperature_c",
        "shock_threshold_g",
        mode="before",
    )
    @classmethod
    def reject_boolean_thresholds(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid thresholds")
        return value


# ---------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------


def connect_db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    )


def ensure_database_exists():
    """
    Create the configured database if it does not exist.

    The PostgreSQL role must have CREATEDB permission.
    """

    params = conninfo_to_dict(DATABASE_URL)
    database_name = params.get("dbname")

    if not database_name:
        raise RuntimeError("DATABASE_URL must specify a database name")

    maintenance_params = dict(params)
    maintenance_params["dbname"] = "postgres"

    with psycopg.connect(
        **maintenance_params,
        autocommit=True,
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        ).fetchone()

        if exists is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )
            logger.info("Created database %s", database_name)
        else:
            logger.info("Database %s already exists", database_name)


def init_db():
    """
    Create the current schema if the tables do not exist.

    This function intentionally does not migrate or alter existing tables.
    For this schema to be used on an existing database, that database
    must already have the current schema.
    """

    ensure_database_exists()

    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY
                    CHECK (
                        device_id ~ '^[A-Za-z0-9_-]{1,64}$'
                    ),

                firmware_version TEXT NOT NULL,
                registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                latest_observed_at TIMESTAMPTZ NOT NULL,
                latest_message JSONB NOT NULL,

                max_temperature_c DOUBLE PRECISION NOT NULL
                    CHECK (max_temperature_c BETWEEN -100 AND 100),

                shock_threshold_g DOUBLE PRECISION NOT NULL
                    CHECK (
                        shock_threshold_g > 0
                        AND shock_threshold_g <= 1000
                    )
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                observed_at TIMESTAMPTZ NOT NULL,
                boot_id TEXT NOT NULL,
                message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
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

                type TEXT NOT NULL CHECK (
                    type IN ('temperature_breach', 'shock_detected')
                ),

                severity TEXT NOT NULL DEFAULT 'warning',

                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'acknowledged')),

                observed_at TIMESTAMPTZ NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                acknowledged_at TIMESTAMPTZ
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                boot_id TEXT NOT NULL,
                message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
                processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

                PRIMARY KEY (device_id, boot_id, message_seq)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS telemetry_device_time_idx
            ON telemetry (device_id, observed_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS alerts_device_status_idx
            ON alerts (device_id, status, observed_at DESC)
            """
        )

    logger.info("Database schema is ready")


# ---------------------------------------------------------------------
# MQTT responses
# ---------------------------------------------------------------------


def publish_json(
    topic: str,
    payload: dict,
    *,
    retain: bool = False,
):
    """Queue a JSON MQTT message."""

    info = mqtt_client.publish(
        topic,
        __import__("json").dumps(payload),
        qos=1,
        retain=retain,
    )

    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error("Could not queue MQTT message for %s", topic)


def publish_registration_ack(
    device_id: str,
    *,
    status: str,
    settings: dict | None = None,
    reason: str | None = None,
):
    payload = {
        "message_type": "registration_ack",
        "device_id": device_id,
        "status": status,
    }

    if settings is not None:
        payload["settings"] = settings

    if reason is not None:
        payload["reason"] = reason

    publish_json(
        f"coldchain/{device_id}/registration/ack",
        payload,
    )


def publish_device_config(device_id: str, settings: dict):
    """Publish the current settings as a retained MQTT message."""

    publish_json(
        f"coldchain/{device_id}/config",
        {
            "message_type": "device_config",
            "device_id": device_id,
            "settings": settings,
        },
        retain=True,
    )


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------


def handle_registration(message: DeviceMessage, data: dict):
    """
    Register a new device with its supplied settings.

    Re-registration does not overwrite settings already stored in
    PostgreSQL. The acknowledgement returns the stored configuration.
    """

    try:
        settings = DeviceSettings.model_validate(message.payload.get("settings", {}))
    except ValidationError as exc:
        publish_registration_ack(
            message.device_id,
            status="rejected",
            reason=(
                "Registration requires valid max_temperature_c "
                "and shock_threshold_g settings."
            ),
        )
        logger.warning(
            "Invalid registration settings from %s: %s",
            message.device_id,
            exc,
        )
        return

    requested = settings.model_dump()

    with connect_db() as conn:
        inserted = conn.execute(
            """
            INSERT INTO devices (
                device_id,
                firmware_version,
                last_seen,
                latest_observed_at,
                latest_message,
                max_temperature_c,
                shock_threshold_g
            )
            VALUES (%s, %s, now(), %s, %s, %s, %s)

            ON CONFLICT (device_id) DO NOTHING

            RETURNING device_id
            """,
            (
                message.device_id,
                message.firmware_version,
                message.observed_at,
                Jsonb(data),
                requested["max_temperature_c"],
                requested["shock_threshold_g"],
            ),
        ).fetchone()

        is_new = inserted is not None

        if not is_new:
            # Preserve the existing thresholds when a device reconnects.
            conn.execute(
                """
                UPDATE devices
                SET firmware_version = %s,
                    last_seen = now(),
                    latest_observed_at = GREATEST(
                        latest_observed_at, %s
                    ),
                    latest_message = CASE
                        WHEN %s >= latest_observed_at
                        THEN %s
                        ELSE latest_message
                    END
                WHERE device_id = %s
                """,
                (
                    message.firmware_version,
                    message.observed_at,
                    message.observed_at,
                    Jsonb(data),
                    message.device_id,
                ),
            )

        row = conn.execute(
            """
            SELECT max_temperature_c, shock_threshold_g
            FROM devices
            WHERE device_id = %s
            """,
            (message.device_id,),
        ).fetchone()

    stored_settings = {
        "max_temperature_c": row["max_temperature_c"],
        "shock_threshold_g": row["shock_threshold_g"],
    }

    status = "registered" if is_new else "already_registered"

    publish_registration_ack(
        message.device_id,
        status=status,
        settings=stored_settings,
    )

    publish_device_config(message.device_id, stored_settings)

    logger.info(
        "Device %s registration status=%s settings=%s",
        message.device_id,
        status,
        stored_settings,
    )


# ---------------------------------------------------------------------
# Alert storage
# ---------------------------------------------------------------------


def insert_alert(
    conn,
    *,
    device_id: str,
    alert_type: str,
    observed_at: datetime,
    details: dict,
):
    """
    Store an alert explicitly reported by the ESP32.

    No temperature or acceleration threshold is evaluated here.
    """

    alert_id = f"alert-{uuid.uuid4().hex[:8]}"

    conn.execute(
        """
        INSERT INTO alerts (
            id,
            device_id,
            type,
            severity,
            status,
            observed_at,
            details
        )
        VALUES (%s, %s, %s, 'warning', 'open', %s, %s)
        """,
        (
            alert_id,
            device_id,
            alert_type,
            observed_at,
            Jsonb(details),
        ),
    )

    logger.info(
        "Stored device-reported alert %s for %s",
        alert_type,
        device_id,
    )


# ---------------------------------------------------------------------
# MQTT message processing
# ---------------------------------------------------------------------


def handle_mqtt_message(client, userdata, mqtt_message):
    """
    Handle device registration, telemetry, and reported alert events.

    Only registration can create a device. The backend never decides
    whether a temperature or acceleration reading is an alert.
    """

    try:
        message = DeviceMessage.model_validate_json(mqtt_message.payload)

        # Expected message topic:
        # coldchain/<device_id>/messages
        parts = mqtt_message.topic.split("/")

        if len(parts) != 3 or parts[0] != "coldchain" or parts[2] != "messages":
            logger.warning(
                "Rejected unexpected MQTT topic: %s",
                mqtt_message.topic,
            )
            return

        topic_device_id = parts[1]

        if not DEVICE_ID_PATTERN.fullmatch(message.device_id):
            logger.warning("Rejected invalid device ID")
            return

        if topic_device_id != message.device_id:
            logger.warning(
                "Rejected topic/payload ID mismatch: topic=%s payload=%s",
                topic_device_id,
                message.device_id,
            )
            return

        if not message.boot_id or not message.firmware_version:
            logger.warning(
                "Rejected missing metadata from %s",
                message.device_id,
            )
            return

        data = message.model_dump(mode="json")

        # Only a registration message can create a device.
        if message.message_type == "register":
            handle_registration(message, data)
            return

        with connect_db() as conn:
            device = conn.execute(
                """
                SELECT device_id
                FROM devices
                WHERE device_id = %s
                """,
                (message.device_id,),
            ).fetchone()

            if device is None:
                logger.warning(
                    "Rejected %s from unregistered device %s",
                    message.message_type,
                    message.device_id,
                )
                return

            event_type = None

            if message.message_type == "event":
                event_type = message.payload.get("type")

                if event_type not in {
                    "temperature_breach",
                    "shock_detected",
                }:
                    logger.warning(
                        "Rejected unsupported event type %r from %s",
                        event_type,
                        message.device_id,
                    )
                    return

            # Update presence and the latest message from this device.
            conn.execute(
                """
                UPDATE devices
                SET firmware_version = %s,
                    last_seen = now(),
                    latest_observed_at = GREATEST(
                        latest_observed_at, %s
                    ),
                    latest_message = CASE
                        WHEN %s >= latest_observed_at
                        THEN %s
                        ELSE latest_message
                    END
                WHERE device_id = %s
                """,
                (
                    message.firmware_version,
                    message.observed_at,
                    message.observed_at,
                    Jsonb(data),
                    message.device_id,
                ),
            )

            # MQTT QoS 1 may redeliver messages. Process each identity
            # (device, boot_id, message_seq) only once.
            inserted = conn.execute(
                """
                INSERT INTO processed_messages (
                    device_id,
                    boot_id,
                    message_seq
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (device_id, boot_id, message_seq)
                DO NOTHING
                RETURNING device_id
                """,
                (
                    message.device_id,
                    message.boot_id,
                    message.message_seq,
                ),
            ).fetchone()

            if inserted is None:
                logger.debug(
                    "Ignoring duplicate message from %s",
                    message.device_id,
                )
                return

            if message.message_type == "telemetry":
                # Store complete telemetry; no threshold comparisons.
                conn.execute(
                    """
                    INSERT INTO telemetry (
                        device_id,
                        observed_at,
                        boot_id,
                        message_seq,
                        data
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        message.device_id,
                        message.observed_at,
                        message.boot_id,
                        message.message_seq,
                        Jsonb(data),
                    ),
                )

            elif message.message_type == "event":
                # The ESP32 has already detected the alert.
                details = {
                    key: value
                    for key, value in message.payload.items()
                    if key != "type"
                }

                insert_alert(
                    conn,
                    device_id=message.device_id,
                    alert_type=event_type,
                    observed_at=message.observed_at,
                    details=details,
                )

    except (ValidationError, ValueError) as exc:
        logger.warning(
            "Rejected invalid MQTT message on %s: %s",
            mqtt_message.topic,
            exc,
        )
    except Exception:
        logger.exception(
            "Failed processing MQTT message on %s",
            mqtt_message.topic,
        )


def handle_mqtt_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties,
):
    if reason_code.is_failure:
        logger.error("MQTT connection failed: %s", reason_code)
        return

    logger.info(
        "Connected to MQTT broker %s:%s",
        MQTT_HOST,
        MQTT_PORT,
    )
    client.subscribe(MQTT_TOPIC, qos=1)
    logger.info("Subscribed to MQTT topic %s", MQTT_TOPIC)


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="coldchain-backend",
)

if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

mqtt_client.on_connect = handle_mqtt_connect
mqtt_client.on_message = handle_mqtt_message


# ---------------------------------------------------------------------
# FastAPI lifecycle
# ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the database and current tables if missing.
    # No legacy migrations or ALTER TABLE operations are performed.
    init_db()

    mqtt_client.connect_async(
        MQTT_HOST,
        MQTT_PORT,
        keepalive=60,
    )
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


# ---------------------------------------------------------------------
# Device API
# ---------------------------------------------------------------------


@app.get("/api/v1/devices")
def list_devices():
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT device_id, last_seen
            FROM devices
            ORDER BY device_id
            """
        ).fetchall()

    now = datetime.now(timezone.utc)

    return {
        "items": [
            {
                "device_id": row["device_id"],
                "status": (
                    "online"
                    if (now - row["last_seen"]).total_seconds()
                    <= ONLINE_TIMEOUT_SECONDS
                    else "offline"
                ),
            }
            for row in rows
        ]
    }


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT latest_message
            FROM devices
            WHERE device_id = %s
            """,
            (device_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    return row["latest_message"]


@app.get("/api/v1/devices/{device_id}/settings")
def get_device_settings(device_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT device_id, max_temperature_c, shock_threshold_g
            FROM devices
            WHERE device_id = %s
            """,
            (device_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    return row


@app.patch("/api/v1/devices/{device_id}/settings")
def update_device_settings(
    device_id: str,
    settings: DeviceSettingsUpdate,
):
    supplied = settings.model_fields_set
    updates = {}

    if "max_temperature_c" in supplied:
        if settings.max_temperature_c is None:
            raise HTTPException(
                status_code=422,
                detail="max_temperature_c cannot be null",
            )
        updates["max_temperature_c"] = settings.max_temperature_c

    if "shock_threshold_g" in supplied:
        if settings.shock_threshold_g is None:
            raise HTTPException(
                status_code=422,
                detail="shock_threshold_g cannot be null",
            )
        updates["shock_threshold_g"] = settings.shock_threshold_g

    if not updates:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one threshold",
        )

    # Only these known column names may be updated.
    assignments = ", ".join(f"{column} = %s" for column in updates)
    values = list(updates.values()) + [device_id]

    with connect_db() as conn:
        row = conn.execute(
            f"""
            UPDATE devices
            SET {assignments}
            WHERE device_id = %s
            RETURNING device_id, max_temperature_c, shock_threshold_g
            """,
            values,
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    config = {
        "max_temperature_c": row["max_temperature_c"],
        "shock_threshold_g": row["shock_threshold_g"],
    }

    # Publish updated settings for the ESP32 to apply.
    publish_device_config(device_id, config)

    return {
        "device_id": device_id,
        **config,
    }


@app.get("/api/v1/devices/{device_id}/telemetry")
def get_telemetry(
    device_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT data
            FROM telemetry
            WHERE device_id = %s
            ORDER BY observed_at DESC, id DESC
            LIMIT %s
            """,
            (device_id, limit),
        ).fetchall()

    return {
        "items": [row["data"] for row in rows],
    }


# ---------------------------------------------------------------------
# Alert API
# ---------------------------------------------------------------------


@app.get("/api/v1/alerts")
def get_alerts(
    status: Literal["open", "acknowledged"] | None = None,
    device_id: str | None = None,
):
    conditions = []
    params = []

    if status is not None:
        conditions.append("status = %s")
        params.append(status)

    if device_id is not None:
        conditions.append("device_id = %s")
        params.append(device_id)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    query = (
        """
        SELECT
            id,
            device_id,
            type,
            severity,
            status,
            observed_at,
            details,
            acknowledged_at
        FROM alerts
        """
        + where_clause
        + " ORDER BY observed_at DESC"
    )

    with connect_db() as conn:
        rows = conn.execute(query, params).fetchall()

    return {"items": rows}


@app.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            UPDATE alerts
            SET status = 'acknowledged',
                acknowledged_at = COALESCE(
                    acknowledged_at, now()
                )
            WHERE id = %s
            RETURNING id, status, acknowledged_at
            """,
            (alert_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Alert not found",
        )

    return {
        "alert_id": row["id"],
        "status": row["status"],
        "acknowledged_at": row["acknowledged_at"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
