# Cold-chain telemetry backend

A small backend for an ESP32 cold-chain / predictive-maintenance project.

## Stack

- **ESP32 firmware** publishes sensor messages over MQTT.
- **Mosquitto** is the MQTT broker.
- **Paho MQTT** receives messages in the Python backend.
- **Pydantic** validates the common JSON message envelope.
- **PostgreSQL** stores devices, telemetry, and alerts.
- **Psycopg 3** connects Python to PostgreSQL.
- **FastAPI + Uvicorn** provide the HTTP API.
- **uv** manages Python dependencies and the virtual environment.
- **Podman** runs PostgreSQL in a container; the Python API runs on the host.

This README assumes Debian 13 or a similar Debian-based Linux system. Commands using `uv` should be run from this project's root directory (the directory containing `main.py` and `pyproject.toml`).

## 1. Install the tools

Install Podman and Mosquitto:

```bash
sudo apt update
sudo apt install podman mosquitto
```

Install `uv` if needed, following the official guide: <https://docs.astral.sh/uv/getting-started/installation/>.

Start the host's Mosquitto service:

```bash
sudo systemctl enable --now mosquitto
```

The backend and test publisher connect to `127.0.0.1:1883`. This assumes Mosquitto is running on the same machine as Python.

## 2. Start PostgreSQL with Podman

### First-time setup

Run this command once to create the PostgreSQL container and its persistent data volume:

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

This initializes a database named `coldchain` and a login role named `coldchain` when the volume is empty. The port is bound to `127.0.0.1`, so PostgreSQL is exposed only on this machine. The named volume `coldchain_pgdata` keeps data when the container is stopped or recreated.

**If you already ran the command above, do not run `podman run` again.** Start the existing container instead:

```bash
podman start coldchain-postgres
```

Check that it is running and ready:

```bash
podman ps
podman exec coldchain-postgres pg_isready -U coldchain -d coldchain
```

Expected readiness output includes `accepting connections`.

Useful container commands:

```bash
# Stop / start PostgreSQL
podman stop coldchain-postgres
podman start coldchain-postgres

# Inspect PostgreSQL logs
podman logs -f coldchain-postgres
```

Press `Ctrl+C` to stop following the logs; that does not stop the container.

### Database and table creation

The container image creates the **database** (`coldchain`) and **database role** (`coldchain`) on its first initialization. The backend creates the **tables** automatically when FastAPI starts. You do not need to create those tables manually.

After starting the backend (see Section 4), you can inspect the tables with:

```bash
podman exec -it coldchain-postgres psql -U coldchain -d coldchain
```

At the `psql` prompt, enter:

```sql
\dt
```

The tables should include `devices`, `telemetry`, and `alerts`. To leave `psql`, enter `\q`.

> PostgreSQL's `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` variables only initialize an empty data directory. Changing these variables later does not change credentials in an already-initialized volume. Do not remove `coldchain_pgdata` unless you intend to delete the stored database data.

## 3. Configure the application

Create your local environment file:

```bash
cp .env.example .env
```

Set the database URL in `.env` to match the Podman command above:

```dotenv
DATABASE_URL=postgresql://coldchain:dev_only_change_me@127.0.0.1:5432/coldchain
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC=coldchain/+/messages
MAX_TEMPERATURE_C=8.0
ONLINE_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO
DEVICE_ID=tracker-001
```

The example password is only for local testing. Change it if you change the password in PostgreSQL. Keep `.env` private and do not commit it to version control.

The environment variables mean:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `MQTT_HOST`, `MQTT_PORT` | Address and port of Mosquitto as seen by the backend |
| `MQTT_TOPIC` | MQTT subscription filter; `coldchain/+/messages` matches one device-topic segment |
| `MAX_TEMPERATURE_C` | Temperature above which a warning alert is created; default `8.0` |
| `ONLINE_TIMEOUT_SECONDS` | Time since last message before a device is marked offline; default `60` seconds |
| `LOG_LEVEL` | Python logging level |
| `DEVICE_ID` | Device ID used by the sample publisher only |

`uv run --env-file .env ...` loads these variables for that command; you do not need `python-dotenv`.

## 4. Install dependencies and start the API

From the project root:

```bash
uv sync
uv run --env-file .env uvicorn main:app --reload
```

`uv sync` creates/updates the local `.venv` and resolves dependencies from `pyproject.toml`. Commit `uv.lock` when it is generated so teammates can use the same resolved versions. Do not commit `.venv/` or `.env`.

On startup, the application connects to PostgreSQL and creates the required tables if they do not exist. It then starts an MQTT client and subscribes to `coldchain/+/messages`. Wait until the log shows a connection to Mosquitto and the subscription before publishing test messages.

Expected log messages look similar to:

```text
Application startup complete.
Connected to MQTT broker 127.0.0.1:1883
Subscribed to MQTT topic coldchain/+/messages
```

The application serves HTTP on <http://127.0.0.1:8000>.

## 5. Test the API in your browser (no frontend required)

FastAPI automatically creates interactive API documentation. It is a testing interface, not a frontend for the final product.

Open these links while the backend is running:

- **[Interactive API documentation — `/docs`](http://127.0.0.1:8000/docs)**: use this for all endpoints, including POST requests.
- [Alternative documentation — `/redoc`](http://127.0.0.1:8000/redoc)
- [Health check — `/health`](http://127.0.0.1:8000/health)
- [List devices](http://127.0.0.1:8000/api/v1/devices)
- [List all alerts](http://127.0.0.1:8000/api/v1/alerts)
- [List open alerts](http://127.0.0.1:8000/api/v1/alerts?status=open)
- [Open alerts for `tracker-001`](http://127.0.0.1:8000/api/v1/alerts?device_id=tracker-001&status=open)

For a GET endpoint, you can open its URL directly. To use the interactive page, open `/docs`, expand an endpoint, click **Try it out**, fill in any parameters, then click **Execute**. The response status and JSON body will appear below it.

Use `/docs` for `POST /api/v1/alerts/{alert_id}/acknowledge`; ordinary browser address-bar navigation only sends GET requests. First list the alerts and copy an actual alert ID, then enter it in the POST endpoint's `alert_id` field and execute the request.

A `404` from `/` or `/favicon.ico` is expected because this project has no homepage or configured favicon. `/v1/models` is not one of this project's routes. The API routes use the `/api/v1/` prefix, except for `/health`.

## 6. Test the full MQTT → PostgreSQL → HTTP API pipeline

Keep Uvicorn running in the first terminal. Open a **second terminal** in the project root and run:

```bash
uv run --env-file .env python publish_test.py
```

The sample publisher sends two messages to `coldchain/tracker-001/messages` (or to the device ID configured with `DEVICE_ID`):

1. A telemetry message with a valid temperature of `9.1 °C`.
2. A `shock_detected` event.

With the default `MAX_TEMPERATURE_C=8.0`, these should create `temperature_breach` and `shock_detected` alerts. The test publisher sends synthetic messages once; actual firmware should publish routine telemetry at its configured interval (for example, every 15 seconds).

After publishing, refresh these browser links:

- [Device list](http://127.0.0.1:8000/api/v1/devices) — `tracker-001` should appear.
- [Latest message from `tracker-001`](http://127.0.0.1:8000/api/v1/devices/tracker-001) — returns the latest complete message envelope.
- [Recent telemetry (limit 100)](http://127.0.0.1:8000/api/v1/devices/tracker-001/telemetry?limit=100) — returns stored telemetry messages.
- [All alerts](http://127.0.0.1:8000/api/v1/alerts) — should include the generated alerts.
- [Open alerts](http://127.0.0.1:8000/api/v1/alerts?status=open)

The device endpoint can return `404 Device not found` before the first valid MQTT message arrives. Likewise, telemetry and alert lists may be empty before test data is published.

## 7. HTTP API reference

| Method | Endpoint | Behaviour |
|---|---|---|
| `GET` | `/api/v1/devices` | Lists known devices with `online` / `offline` status |
| `GET` | `/api/v1/devices/{device_id}` | Returns the latest complete JSON message from a device (telemetry or event) |
| `GET` | `/api/v1/devices/{device_id}/telemetry?limit=100` | Returns recent telemetry envelopes, newest first; limit must be 1–1000 |
| `GET` | `/api/v1/alerts` | Lists all alerts |
| `GET` | `/api/v1/alerts?status=open` | Filters alerts by status |
| `GET` | `/api/v1/alerts?device_id=tracker-001&status=open` | Filters alerts by device and status; either filter can be omitted |
| `POST` | `/api/v1/alerts/{alert_id}/acknowledge` | Marks an alert `acknowledged`; it does not delete or resolve the alert |
| `GET` | `/health` | Returns `{"status":"ok"}` when the process is running |

List endpoints return an object with an `items` array. Telemetry entries preserve the full device message, including `schema_version`, `message_type`, `device_id`, `firmware_version`, `boot_id`, `message_seq`, `observed_at`, `uptime_ms`, and `payload`.

## 8. MQTT topic and device messages

The backend subscribes to:

```text
coldchain/+/messages
```

A device publishes to its own topic, for example:

```text
coldchain/tracker-001/messages
```

The JSON body follows the common envelope. In abbreviated form:

```json
{
  "schema_version": 1,
  "message_type": "telemetry",
  "device_id": "tracker-001",
  "firmware_version": "0.1.0",
  "boot_id": "7f3a9c21",
  "message_seq": 42,
  "observed_at": "2026-10-24T12:30:15Z",
  "uptime_ms": 120000,
  "payload": {
    "temperature": {"valid": true, "celsius": 5.4},
    "motion": {"valid": true, "peak_acceleration_magnitude_g": 1.08},
    "location": {"fix_valid": false, "latitude": null, "longitude": null}
  }
}
```

The common envelope is validated by Pydantic. The `payload` is kept flexible to support different sensor/event payloads. Routine telemetry is persisted in PostgreSQL. A valid temperature reading above the configured threshold creates a warning alert; `shock_detected` and `temperature_breach` event messages also create alerts.

For an ESP32 on Wi-Fi, use the backend machine's **LAN IP address** as the MQTT host in firmware—not `localhost` or `127.0.0.1`, which refer to the ESP32 itself. Configure Mosquitto to accept LAN clients and use authentication before connecting devices on a shared network. Do not expose an unauthenticated MQTT broker to the public internet.

## 9. Troubleshooting

### `podman run` says the container name is already in use

The container already exists. Start it instead of creating it again:

```bash
podman start coldchain-postgres
```

### PostgreSQL connection refused

Check that the container is running and ready:

```bash
podman ps
podman logs coldchain-postgres
podman exec coldchain-postgres pg_isready -U coldchain -d coldchain
```

Confirm `DATABASE_URL` in `.env` uses the same username, password, host, port, and database as the container. If a native PostgreSQL service is already bound to port `5432`, stop that service or use a different host port for the container.

### `password authentication failed`

The credentials in `.env` do not match the role's actual password. Remember that changing `POSTGRES_PASSWORD` on an already-initialized container does not reset its password. Do not delete the volume just to change a password if you need to keep existing data.

### Device or alert lists are empty

Make sure Mosquitto is running, the backend log says it connected and subscribed, and `MQTT_HOST` / `MQTT_PORT` match the broker. Then run `publish_test.py` in another terminal and refresh the endpoints. The device registry only contains devices that have sent a valid message.

### Tables are missing

Make sure FastAPI started successfully and `DATABASE_URL` points to the intended database. The application creates tables during startup. Connect with `psql` using the command in Section 2 and inspect them with `\dt`.

### The browser shows `404 Not Found`

Check that the URL is one of the documented paths. This project does not define `/`; use `/docs` for interactive documentation, `/health` for the health check, or `/api/v1/...` for the device and alert API.

## 10. Current limitations

This is a hackathon starter, not a production deployment. Before production use, add API authentication/authorization, authenticated MQTT and broker access controls, stronger type-specific payload validation, database migrations, tests, retention policies, monitoring, and deployment secrets management. Devices are registered after their first valid message; offline status is based on the most recent message received (60 seconds by default). Telemetry is deduplicated by `(device_id, boot_id, message_seq)`. Repeated open temperature alerts are suppressed per device; acknowledging an alert does not resolve or delete it.
