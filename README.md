# Cold-chain telemetry backend

Minimal FastAPI + Paho MQTT + PostgreSQL example for the ESP32 project.

## 1. Install and configure

Create a PostgreSQL database named `coldchain`, then install dependencies:

```bash
createdb coldchain
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then export its values in your shell (the application does not automatically load `.env`):

```bash
cp .env.example .env
set -a
source .env
set +a
```

For the defaults shown there, PostgreSQL must accept `postgres` / `postgres` on localhost. Change `DATABASE_URL` to match your local setup.

Start Mosquitto on the backend machine:

```bash
sudo systemctl enable --now mosquitto
```

Then start the HTTP API:

```bash
uvicorn main:app --reload
```

The application creates its tables at startup. Open `http://127.0.0.1:8000/docs` for interactive API documentation.

In a second terminal, with the same environment active, send test telemetry and a shock event:

```bash
python publish_test.py
```

The demo temperature is 9.1 °C, so it should generate a `temperature_breach` alert when the configured limit is 8.0 °C.

## 2. MQTT message topic

The backend subscribes to `coldchain/+/messages`. Publish both telemetry and event envelopes to a device-specific topic, for example:

```text
coldchain/tracker-001/messages
```

The JSON body must contain the common envelope from the project specification. The backend validates it with Pydantic. Telemetry messages are stored in PostgreSQL; shock events and temperature readings above `MAX_TEMPERATURE_C` create alerts.

For an ESP32 on Wi-Fi, `MQTT_HOST` must be the backend machine's LAN IP address, not `127.0.0.1` or `localhost`. Configure Mosquitto to accept LAN clients and use broker authentication; do not expose an unauthenticated broker to the public internet.

## 3. Routes

- `GET /api/v1/devices` — devices seen at least once, with online/offline state based on the last received message.
- `GET /api/v1/devices/{device_id}` — latest complete hardware JSON message (telemetry or event).
- `GET /api/v1/devices/{device_id}/telemetry?limit=100` — latest telemetry messages, newest first.
- `GET /api/v1/alerts?status=open&device_id=tracker-001` — optional filters; either can be omitted.
- `POST /api/v1/alerts/{alert_id}/acknowledge` — changes alert status to `acknowledged`; it does not delete or resolve it.

`items` is a JSON array of objects, not a comma-separated string. The online timeout defaults to 60 seconds for devices reporting every 15 seconds.

## Notes

- Device entries appear after a device sends its first valid MQTT message. A device that has never sent a message will not appear until registered/seeded in the database.
- The example deduplicates open temperature-breach alerts per device. Acknowledging an alert does not resolve it.
- This is a hackathon starter, not a production-secured service. Add MQTT authentication, API authentication, stricter payload models, migrations, and operational monitoring before deployment.
