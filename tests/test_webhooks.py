import hashlib
import hmac
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.delivery import RETRY_DELAYS, process_one_due_event
from app.main import create_app
from app.models import WebhookEvent
from app.signing import sign_payload, verify_signature

SECRET = "test-signing-secret"


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        webhook_secret=SECRET,
        http_timeout_seconds=1,
        worker_poll_interval_seconds=0.01,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client, app.state.session_factory


def create_event(client, webhook_url="http://webhook.test/target"):
    response = client.post(
        "/events",
        json={
            "type": "payment.failed",
            "payload": {"payment_id": "p-123"},
            "webhook_url": webhook_url,
        },
    )
    assert response.status_code == 201
    return response.json()


def attempt_shape(attempt):
    assert set(attempt) == {"attempted_at", "http_status", "outcome"}


def event_shape(event):
    assert set(event) == {
        "id",
        "type",
        "payload",
        "webhook_url",
        "status",
        "created_at",
        "attempts",
    }
    for attempt in event["attempts"]:
        attempt_shape(attempt)


def test_api_event_endpoints_and_response_shapes(api):
    client, _ = api
    created = create_event(client)
    event_shape(created)
    assert created["status"] == "pending"
    assert created["attempts"] == []

    listed = client.get("/events")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    event_shape(listed.json()[0])

    fetched = client.get(f"/events/{created['id']}")
    assert fetched.status_code == 200
    event_shape(fetched.json())
    assert fetched.json()["id"] == created["id"]

    assert client.post(f"/events/{created['id']}/retry").status_code == 400
    assert client.get("/events/999999").status_code == 404
    assert client.post("/events/999999/retry").status_code == 404


def test_hmac_sha256_signature_format_and_verification():
    body = b'{"type":"user.signup","payload":{"id":7}}'
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    signature = sign_payload(body, SECRET)
    assert signature == f"sha256={expected}"
    assert verify_signature(body, signature, SECRET)
    assert not verify_signature(body + b" ", signature, SECRET)


def test_successful_delivery_is_signed_and_recorded(api):
    client, session_factory = api
    event = create_event(client)
    captured = {}

    def receive(request):
        captured["body"] = request.content
        captured["signature"] = request.headers["X-Webhook-Signature"]
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receive)) as outbound:
        assert process_one_due_event(
            session_factory, outbound, SECRET
        )

    assert verify_signature(captured["body"], captured["signature"], SECRET)
    detail = client.get(f"/events/{event['id']}").json()
    event_shape(detail)
    assert detail["status"] == "delivered"
    assert detail["attempts"] == [
        {
            "attempted_at": detail["attempts"][0]["attempted_at"],
            "http_status": 204,
            "outcome": "success",
        }
    ]
    assert not process_one_due_event(session_factory, outbound, SECRET)


def test_failed_http_responses_obey_exact_retry_schedule_and_dead_status(api):
    client, session_factory = api
    event = create_event(client)

    def reject(_request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(reject)) as outbound:
        with session_factory() as session:
            stored = session.get(WebhookEvent, event["id"])
            first_attempt_at = stored.next_attempt_at + timedelta(seconds=1)

        assert process_one_due_event(
            session_factory, outbound, SECRET, first_attempt_at
        )
        assert not process_one_due_event(
            session_factory,
            outbound,
            SECRET,
            first_attempt_at + RETRY_DELAYS[0] - timedelta(milliseconds=1),
        )

        second_attempt_at = first_attempt_at + RETRY_DELAYS[0]
        assert process_one_due_event(
            session_factory, outbound, SECRET, second_attempt_at
        )
        assert not process_one_due_event(
            session_factory,
            outbound,
            SECRET,
            second_attempt_at + RETRY_DELAYS[1] - timedelta(milliseconds=1),
        )

        third_attempt_at = second_attempt_at + RETRY_DELAYS[1]
        assert process_one_due_event(
            session_factory, outbound, SECRET, third_attempt_at
        )
        assert not process_one_due_event(
            session_factory,
            outbound,
            SECRET,
            third_attempt_at + RETRY_DELAYS[2] - timedelta(milliseconds=1),
        )

        fourth_attempt_at = third_attempt_at + RETRY_DELAYS[2]
        assert process_one_due_event(
            session_factory, outbound, SECRET, fourth_attempt_at
        )
        assert not process_one_due_event(
            session_factory,
            outbound,
            SECRET,
            fourth_attempt_at + timedelta(days=1),
        )

    detail = client.get(f"/events/{event['id']}").json()
    event_shape(detail)
    assert detail["status"] == "dead"
    assert len(detail["attempts"]) == 4
    assert [attempt["http_status"] for attempt in detail["attempts"]] == [
        503,
        503,
        503,
        503,
    ]
    assert [attempt["outcome"] for attempt in detail["attempts"]] == [
        "failed",
        "failed",
        "failed",
        "failed",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: httpx.ReadTimeout("timed out", request=request),
        lambda request: httpx.ConnectError("connection failed", request=request),
    ],
    ids=["timeout", "network-error"],
)
def test_timeout_and_network_errors_record_null_status(api, failure):
    client, session_factory = api
    event = create_event(client)

    def fail(request):
        raise failure(request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as outbound:
        assert process_one_due_event(session_factory, outbound, SECRET)

    detail = client.get(f"/events/{event['id']}").json()
    event_shape(detail)
    assert detail["status"] == "failed"
    assert detail["attempts"][0]["http_status"] is None
    assert detail["attempts"][0]["outcome"] == "failed"


def test_manual_retry_of_dead_event_preserves_history_and_can_deliver(api):
    client, session_factory = api
    event = create_event(client)

    def reject(_request):
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(reject)) as outbound:
        with session_factory() as session:
            stored = session.get(WebhookEvent, event["id"])
            attempt_at = stored.next_attempt_at + timedelta(seconds=1)
        for attempt_number in range(4):
            assert process_one_due_event(
                session_factory, outbound, SECRET, attempt_at
            )
            if attempt_number < len(RETRY_DELAYS):
                attempt_at += RETRY_DELAYS[attempt_number]

    dead = client.get(f"/events/{event['id']}").json()
    assert dead["status"] == "dead"
    assert len(dead["attempts"]) == 4

    retried_response = client.post(f"/events/{event['id']}/retry")
    assert retried_response.status_code == 200
    retried = retried_response.json()
    event_shape(retried)
    assert retried["status"] == "pending"
    assert len(retried["attempts"]) == 4

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as outbound:
        assert process_one_due_event(session_factory, outbound, SECRET)

    delivered = client.get(f"/events/{event['id']}").json()
    event_shape(delivered)
    assert delivered["status"] == "delivered"
    assert len(delivered["attempts"]) == 5
    assert delivered["attempts"][-1]["outcome"] == "success"
