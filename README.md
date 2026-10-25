# Cold-chain Telemetry Backend

A small backend for an ESP32 cold-chain monitoring project. The ESP32 registers itself with its own settings, publishes telemetry over MQTT, and reports alert events when its local firmware detects them. The Python backend persists messages and exposes a REST API; it does **not** detect temperature breaches or shocks from sensor readings.

## Stack

- **ESP32 + Arduino framework** — reads sensors, uses its configured thresholds, and publishes registration, telemetry, and event messages.
- **Mosquitto** — MQTT broker, running in Podman.
- **Paho MQTT** — MQTT client used by the backend and test simulator.
- **FastAPI + Uvicorn** — HTTP API.
- **Pydantic** — validates message envelopes and device settings.
- **Psycopg 3** — PostgreSQL driver.
- **PostgreSQL** — stores registered devices, per-device settings, telemetry, and alerts; running in Podman.
- **uv** — Python dependency and virtual-environment manager.

Commands below assume Debian 13 or similar and should be run from the project root (where `main.py`, `publish_test.py`, and `pyproject.toml` are located).

## 1. Install tools

```bash
sudo apt update
sudo apt install podman
```

Install `uv` if necessary using the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

If you previously installed native Mosquitto and want to switch to the container, stop and disable it so it does not occupy port 1883:

```bash
sudo systemctl disable --now mosquitto
```

If the service is not installed, this step is not needed.

## 2. Start PostgreSQL in Podman

Create the container and persistent data volume once:

```bash
podman run -d \
  --name coldchain-postgres \
  -e POSTGRES_USER=coldchain \
  -e POSTGRES_PASSWORD=dev_only_change_me \
  -e POSTGRES_DB=coldchain \
  -p 127.0.0.1:5432:5432 \
  -v coldchain_pgdata:/var/lib/postgresql \
  docker.io/library/postgres:18
```

If you already created the container, don't run the `podman run` command again. Start it instead:

```bash
podman start coldchain-postgres
```

Check readiness:

```bash
podman ps
podman exec coldchain-postgres pg_isready -U coldchain -d coldchain
```

Expected readiness output includes `accepting connections`.

Useful commands:

```bash
podman stop coldchain-postgres
podman start coldchain-postgres
podman logs -f coldchain-postgres
```

The named volume `coldchain_pgdata` preserves data when the container stops. Don't delete it unless you intend to discard the database files.

## 3. Run Mosquitto in Podman

The project includes `mosquitto.conf` for **local-only testing**. It allows anonymous MQTT connections, so the container is published only on loopback. Do not expose this anonymous configuration to your LAN or the internet.

If you already created `coldchain-mosquitto`, start it only after confirming that its configuration and port mapping match this README:

```bash
podman start coldchain-mosquitto
```

If the old container has a different port mapping or configuration, recreate the container but keep the named volume:

```bash
podman rm -f coldchain-mosquitto
```

Then run the `podman run` command below. Removing the container without `--volumes` does not remove `coldchain_mosquitto_data`.

For a first-time setup, run from the project root:

```bash
podman run -d \
  --name coldchain-mosquitto \
  -p 127.0.0.1:1883:1883 \
  -v "$PWD/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  -v coldchain_mosquitto_data:/mosquitto/data \
  docker.io/eclipse-mosquitto:2
```

Check the broker:

```bash
podman ps
podman logs coldchain-mosquitto
```

Useful lifecycle commands:

```bash
podman stop coldchain-mosquitto
podman start coldchain-mosquitto
podman logs -f coldchain-mosquitto
```

The named volume `coldchain_mosquitto_data` persists retained MQTT messages and broker state across restarts.

### Connecting a physical ESP32 later

The loopback-only port mapping above is for testing between the host Python process and the broker. An ESP32 cannot reach the host through `127.0.0.1` on its own device.

Before connecting hardware over Wi-Fi, configure Mosquitto authentication, per-device topic ACLs, and preferably TLS. Only then publish the broker on a LAN interface (for example, with `-p 1883:1883`) and set the ESP32's MQTT host to the laptop's LAN IP address. Do **not** simply expose the anonymous development broker to the network. Device ID agreement between topic and JSON payload is a consistency check, not authentication.

## 4. Configure `.env`

Create the local environment file:

```bash
cp .env.example .env
```

The example configuration is:

```dotenv
DATABASE_URL=postgresql://coldchain:dev_only_change_me@127.0.0.1:5432/coldchain

MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC=coldchain/+/messages

# Empty only for isolated localhost testing.
MQTT_USERNAME=
MQTT_PASSWORD=
TEST_MQTT_USERNAME=
TEST_MQTT_PASSWORD=

ONLINE_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO

# Test publisher only; physical ESP32s should derive IDs from hardware.
DEVICE_ID=tracker-001
```

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `MQTT_HOST`, `MQTT_PORT` | Broker address and port as seen by Python |
| `MQTT_TOPIC` | Backend topic filter; `coldchain/+/messages` matches one device segment |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | Optional backend MQTT credentials |
| `TEST_MQTT_USERNAME`, `TEST_MQTT_PASSWORD` | Optional credentials used by `publish_test.py` |
| `ONLINE_TIMEOUT_SECONDS` | Seconds since the last message before a device is marked offline (default `60`) |
| `LOG_LEVEL` | Python logging level |
| `DEVICE_ID` | Simulated device ID used by the test publisher |

There are no global temperature or shock threshold environment variables. Each device provides its settings during registration, and those values are stored in PostgreSQL. Keep `.env` private and do not commit it.

`uv run --env-file .env ...` loads these variables for that process; no `python-dotenv` dependency is needed.

## 5. Database initialization and schema

When FastAPI starts, `main.py`:

1. Connects to the PostgreSQL maintenance database and checks whether the configured database exists.
2. Creates the database if it is missing. The configured PostgreSQL role must have `CREATEDB` privileges.
3. Creates the current tables and indexes if missing.
4. Starts the MQTT client and subscribes to `coldchain/+/messages`.

The official PostgreSQL container initialized with `POSTGRES_USER=coldchain` gives that role sufficient privileges for this project's local development setup. The `POSTGRES_DB=coldchain` variable already creates the database on a fresh container, but the backend can also create it if it is missing.

The current schema has four tables:

| Table | Purpose |
|---|---|
| `devices` | Device ID, registration time, last seen, latest message, temperature limit, and shock threshold |
| `telemetry` | Historical routine telemetry envelopes |
| `alerts` | Explicit alert events reported by registered devices |
| `processed_messages` | Deduplicates repeated MQTT deliveries by device, boot ID, and message sequence |

**Schema behaviour:** `CREATE TABLE IF NOT EXISTS` creates missing tables; it does not migrate an old table to a new structure. If the database contains tables from an incompatible earlier version, reset the development database once (next section) before starting this version. This is destructive.

### Optional one-time reset from the previous schema

Stop the FastAPI server first. The following deletes the whole `coldchain` database, including all registered devices, settings, telemetry, and alerts:

```bash
podman exec -it coldchain-postgres \
  psql -U coldchain -d postgres
```

At the PostgreSQL prompt:

```sql
DROP DATABASE IF EXISTS coldchain WITH (FORCE);
```

Exit with `\q`. Start FastAPI again; the application will recreate the database and current tables. Do not do this if you need to keep existing data.

To inspect tables after startup:

```bash
podman exec -it coldchain-postgres \
  psql -U coldchain -d coldchain
```

Inside `psql`, use `\dt` to list tables and `\d devices` to inspect the device schema. Exit with `\q`.

## 6. Install Python dependencies and start the API

From the project root:

```bash
uv sync
uv run --env-file .env uvicorn main:app --reload
```

`uv sync` creates or updates `.venv` and resolves dependencies from `pyproject.toml`. Keep `uv.lock` in version control once generated. Do not commit `.venv/` or `.env`.

Expected startup logs include messages similar to:

```text
Application startup complete.
Connected to MQTT broker 127.0.0.1:1883
Subscribed to MQTT topic coldchain/+/messages
```

The API runs at `http://127.0.0.1:8000`.

## 7. Device registration flow

A device must register before it can publish telemetry or alert events. Telemetry does not automatically create a device record.

### Registration message

The device generates an ID (for example, from its ESP32 MAC address), loads its initial settings, and publishes a `register` message to `coldchain/<device_id>/messages`.

```json
{
  "schema_version": 1,
  "message_type": "register",
  "device_id": "tracker-001",
  "firmware_version": "0.1.0",
  "boot_id": "7f3a9c21",
  "message_seq": 0,
  "observed_at": "2026-10-25T10:00:00Z",
  "uptime_ms": 0,
  "payload": {
    "settings": {
      "max_temperature_c": 8.0,
      "shock_threshold_g": 4.0
    }
  }
}
```

Both settings are required, validated, and saved on initial registration. The backend has no fallback threshold defaults.

### Registration acknowledgement

The device should subscribe to `coldchain/<device_id>/registration/ack` before publishing its registration message. The backend replies with one of these statuses:

- `registered` — a new device was created.
- `already_registered` — the device existed; the existing saved settings were preserved.
- `rejected` — the registration settings were missing or invalid.

A successful reply includes the canonical settings read from PostgreSQL:

```json
{
  "message_type": "registration_ack",
  "device_id": "tracker-001",
  "status": "registered",
  "settings": {
    "max_temperature_c": 8.0,
    "shock_threshold_g": 4.0
  }
}
```

The backend also publishes a retained configuration message to `coldchain/<device_id>/config`. Firmware should subscribe to this topic and apply changed settings. The HTTP settings endpoint updates PostgreSQL and publishes the new configuration, but it cannot force firmware to apply it; that logic belongs in the ESP32 code.

A MAC-derived ID helps prevent accidental duplication from hardcoded names but is not authentication. Configure MQTT usernames/passwords and per-device ACLs before allowing hardware or other machines to publish on a shared network.

## 8. Telemetry and alert handling

The ESP32 publishes routine telemetry at its chosen interval (for example, every 15 seconds). A telemetry message may contain temperature, motion, location, and other sensor readings.

**The backend never compares telemetry readings to sensor thresholds.** It stores the complete telemetry envelope.

The ESP32 performs local threshold checks. When its firmware detects an event, it publishes a separate `event` message. For example:

```json
{
  "schema_version": 1,
  "message_type": "event",
  "device_id": "tracker-001",
  "firmware_version": "0.1.0",
  "boot_id": "7f3a9c21",
  "message_seq": 43,
  "observed_at": "2026-10-25T10:00:16Z",
  "uptime_ms": 16000,
  "payload": {
    "type": "shock_detected",
    "peak_acceleration_magnitude_g": 5.7,
    "threshold_g": 4.0,
    "duration_ms": 35
  }
}
```

Supported event types are `temperature_breach` and `shock_detected`. The backend records a corresponding alert only after receiving a supported event from a registered device. It does not infer alerts from telemetry. Repeated MQTT deliveries are deduplicated using `(device_id, boot_id, message_seq)`.

The ID in the JSON payload must match the device ID in its MQTT topic. This rejects accidental mismatches but does not prove the sender's identity; broker authentication and ACLs are needed for that.

## 9. Test the full MQTT → PostgreSQL → API flow

Make sure both Podman containers are running and FastAPI is running in the first terminal. Open a second terminal in the project root and execute:

```bash
uv run --env-file .env python publish_test.py
```

The test publisher simulates an ESP32. It:

1. Subscribes to the registration acknowledgement and configuration topics.
2. Publishes registration with simulated hardware-provisioned settings.
3. Waits for the acknowledgement and reads the settings stored by the backend.
4. Publishes normal telemetry.
5. Simulates local temperature and shock detection and publishes explicit event messages.

The comparisons in `publish_test.py` exist solely to simulate the ESP32. The production backend's `main.py` does not perform these comparisons.

With the default `DEVICE_ID=tracker-001`, open the following links in your browser and refresh after publishing:

- [Interactive API docs — `/docs`](http://127.0.0.1:8000/docs)
- [Health check](http://127.0.0.1:8000/health)
- [List devices](http://127.0.0.1:8000/api/v1/devices)
- [Latest message](http://127.0.0.1:8000/api/v1/devices/tracker-001)
- [Device settings](http://127.0.0.1:8000/api/v1/devices/tracker-001/settings)
- [Telemetry history](http://127.0.0.1:8000/api/v1/devices/tracker-001/telemetry?limit=100)
- [All alerts](http://127.0.0.1:8000/api/v1/alerts)
- [Open alerts](http://127.0.0.1:8000/api/v1/alerts?status=open)

FastAPI's `/docs` is an interactive API testing interface, not an application frontend. Use it to execute POST and PATCH requests; entering a link in the browser address bar only sends a GET request.

## 10. HTTP API reference

| Method | Endpoint | Behaviour |
|---|---|---|
| `GET` | `/health` | Reports whether the API process is running |
| `GET` | `/api/v1/devices` | Lists registered devices and online/offline status |
| `GET` | `/api/v1/devices/{device_id}` | Returns the latest complete message from a device |
| `GET` | `/api/v1/devices/{device_id}/settings` | Returns the device's saved thresholds |
| `PATCH` | `/api/v1/devices/{device_id}/settings` | Updates one or both thresholds and publishes config |
| `GET` | `/api/v1/devices/{device_id}/telemetry?limit=100` | Returns recent telemetry, newest first; limit 1–1000 |
| `GET` | `/api/v1/alerts` | Lists all alerts |
| `GET` | `/api/v1/alerts?status=open` | Lists open alerts |
| `GET` | `/api/v1/alerts?device_id=tracker-001&status=open` | Filters alerts by device and status |
| `POST` | `/api/v1/alerts/{alert_id}/acknowledge` | Marks an alert acknowledged without deleting it |

Use [Swagger UI](http://127.0.0.1:8000/docs) to try the settings endpoint. Example request body for `PATCH /api/v1/devices/tracker-001/settings`:

```json
{
  "max_temperature_c": 6.0,
  "shock_threshold_g": 3.5
}
```

The ESP32 must receive the updated configuration from `coldchain/tracker-001/config` and apply it locally.

## 11. Troubleshooting

### A container name is already in use

Start the existing container instead of creating it again:

```bash
podman start coldchain-postgres
podman start coldchain-mosquitto
```

### PostgreSQL connection refused

Check `podman ps`, inspect logs with `podman logs coldchain-postgres`, and make sure `DATABASE_URL` matches the container's credentials and mapped port.

### Database creation permission error

The PostgreSQL user in `DATABASE_URL` needs `CREATEDB` permission. The `coldchain` user created by this project's official PostgreSQL image setup has that privilege.

### Mosquitto fails to start or port 1883 is occupied

If native Mosquitto is running, stop it with `sudo systemctl disable --now mosquitto`. Check `podman logs coldchain-mosquitto` for configuration or volume errors.

### Registration receives no acknowledgement

Ensure the backend is connected to Mosquitto. The device/test client must subscribe to the acknowledgement topic before publishing registration. Verify the broker is allowing the relevant subscriptions and publications.

### Telemetry or alerts are missing

The device must register first. Confirm that `device_id` in the JSON payload matches the ID in `coldchain/<device_id>/messages`, then run `publish_test.py` and refresh the API links.

### The browser shows 404

There is no homepage at `/`. Use `/docs`, `/health`, or the documented `/api/v1/...` routes.

## 12. Current limitations and security

This is a hackathon starter, not a production-ready deployment.

- **Local Mosquitto config:** `mosquitto.conf` allows anonymous access and is intended only for localhost testing. Its Podman port mapping should stay bound to `127.0.0.1` until authentication/ACLs are configured.
- **Hardware connectivity:** a physical ESP32 cannot connect to the host via its own `127.0.0.1`. When moving to LAN access, configure credentials, per-device topic ACLs, and preferably TLS before binding the broker to a LAN interface.
- **Identity:** MAC-derived IDs are not secrets and can be spoofed. Use individual MQTT credentials and topic permissions to reduce impersonation.
- **Configuration application:** the backend persists settings and publishes them, but device firmware must subscribe to config and apply it.
- **Schema:** startup creates missing tables but does not migrate older schemas. Reset incompatible development databases before switching to this version.
- **Health checks:** `/health` reports that the API process responds; it is not a comprehensive dependency health check.
- **Production hardening:** add API authentication, stronger type-specific payload validation, automated tests, database migrations, retention policies, monitoring, and managed secrets before production use.

Keep `pyproject.toml` and `uv.lock` in version control. Do not commit `.env` or `.venv/`.
