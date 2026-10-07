# Webhook Delivery Engine

A small FastAPI service that persists webhook events in SQLite and delivers them
from a separate polling worker. Retry scheduling is implemented in the
application; no queue library is used.

**Deployment URL:** Not deployed. No live deployment has been provisioned.

## Requirements

- Python 3.10 or newer
- `pip`

## Installation and setup

From the project directory, create and activate a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

On macOS/Linux, use `python3 -m venv .venv`, activate with
`source .venv/bin/activate`, and copy the example with `cp .env.example .env`.
Set `WEBHOOK_SECRET` in `.env` to a private, randomly generated value before
using the service outside local development. `DATABASE_URL` defaults to a
SQLite database file named `webhook.db` in the working directory.

## Running the API and worker

Run each process in a separate terminal from this directory, with the same
`.env` configuration:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.worker
```

The worker polls SQLite for due events. It runs independently of API requests;
run exactly one worker process against a given database.

## API

### Create an event

```powershell
curl.exe -X POST http://127.0.0.1:8000/events `
  -H "Content-Type: application/json" `
  -d '{"type":"user.signup","payload":{"user_id":123},"webhook_url":"https://example.com/webhooks"}'
```

Returns `201` with an event object. Its initial status is `pending`, and the
`attempts` array is empty.

### List events

```powershell
curl.exe http://127.0.0.1:8000/events
```

Returns a `200` array of event objects.

### Retrieve an event and its attempt history

```powershell
curl.exe http://127.0.0.1:8000/events/1
```

Returns `200`, or `404` when the event does not exist.

### Retry a dead event

```powershell
curl.exe -X POST http://127.0.0.1:8000/events/1/retry
```

Returns `200` with the re-queued event. The endpoint returns `400` unless the
event is `dead`; unknown IDs return `404`. A manual retry starts a new
four-attempt automatic cycle while retaining the previous attempt history.

Event objects contain exactly `id`, `type`, `payload`, `webhook_url`, `status`,
`created_at`, and `attempts`. Every attempt contains `attempted_at`,
`http_status`, and `outcome`. A timeout or network error has a null
`http_status`.

## HMAC signing and verification

The worker sends JSON with the shape `{"type": "...", "payload": ...}`. It
serializes this body as compact UTF-8 JSON and computes HMAC-SHA256 over those
exact request-body bytes using `WEBHOOK_SECRET`. The header format is:

```text
X-Webhook-Signature: sha256=<lowercase-hex-digest>
```

Example verification in Python, where `raw_body` is the exact received request
body and `signature` is the header value:

```python
import hashlib
import hmac

expected = "sha256=" + hmac.new(
    WEBHOOK_SECRET.encode("utf-8"),
    raw_body,
    hashlib.sha256,
).hexdigest()
is_valid = hmac.compare_digest(expected, signature)
```

Keep the signing key secret and use the same key when verifying the request.

## Retry strategy

The worker selects due `pending` or `failed` events from SQLite. The first
attempt is eligible immediately after ingestion. A 2xx response marks the
event `delivered`. Any other status, timeout, or network error records a failed
attempt; the subsequent attempt becomes eligible after 30 seconds, 5 minutes,
then 30 minutes. Four failed attempts total (the initial attempt plus three
retries) mark the event `dead`; automatic delivery then stops. All attempt
records remain available through the API.

The worker uses regular SQLite polling and application-managed timestamps,
without a queue library. Run only one worker for a database to avoid concurrent
duplicate deliveries.

## Restart behavior

Events, attempt records, retry counts, and next-attempt timestamps are stored
in SQLite. Restarting the API or worker does not reset scheduled retries; when
the worker starts again it continues with the persisted due times. A process
crash during an outbound HTTP request can leave the result unrecorded. On
restart that due event may be delivered again, so delivery is at-least-once in
that crash window and webhook receivers should handle duplicate events.

## Deployment

Deploy the API and worker as two long-running processes sharing the same
persistent SQLite file and `WEBHOOK_SECRET`. Configure the environment
variables from `.env.example`, install `requirements.txt`, then run:

```sh
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker
```

Use a persistent disk for `webhook.db`; ephemeral instance storage loses event
and retry state. Run one worker process only. No live deployment is included
with this source tree.

## Tests

```powershell
python -m pytest -q
```
