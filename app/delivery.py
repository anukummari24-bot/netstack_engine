import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import DeliveryAttempt, WebhookEvent
from app.signing import sign_payload

logger = logging.getLogger(__name__)

RETRY_DELAYS = (
    timedelta(seconds=30),
    timedelta(minutes=5),
    timedelta(minutes=30),
)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
SessionFactory = sessionmaker[Session]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def process_one_due_event(
    session_factory: SessionFactory,
    client: httpx.Client,
    secret: str,
    now: datetime | None = None,
) -> bool:
    current_time = now or utc_now()
    with session_factory() as session:
        event = session.scalar(
            select(WebhookEvent)
            .where(
                WebhookEvent.status.in_(("pending", "failed")),
                WebhookEvent.next_attempt_at <= current_time,
            )
            .order_by(WebhookEvent.next_attempt_at, WebhookEvent.id)
            .limit(1)
        )
        if event is None:
            return False
        event_id = event.id
        event_type = event.type
        payload = event.payload
        webhook_url = event.webhook_url

    body = json.dumps(
        {"type": event_type, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = sign_payload(body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
    }
    try:
        response = client.post(webhook_url, content=body, headers=headers)
        http_status = response.status_code
        succeeded = 200 <= http_status < 300
    except httpx.TimeoutException as error:
        logger.warning("Webhook event %s timed out: %s", event_id, error)
        http_status = None
        succeeded = False
    except httpx.RequestError as error:
        logger.warning("Webhook event %s network error: %s", event_id, error)
        http_status = None
        succeeded = False

    with session_factory() as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            logger.error("Webhook event %s disappeared during delivery", event_id)
            return True

        session.add(
            DeliveryAttempt(
                event_id=event_id,
                attempted_at=current_time,
                http_status=http_status,
                outcome="success" if succeeded else "failed",
            )
        )
        event.attempt_count += 1

        if succeeded:
            event.status = "delivered"
            event.next_attempt_at = None
        elif event.attempt_count >= MAX_ATTEMPTS:
            event.status = "dead"
            event.next_attempt_at = None
        else:
            event.status = "failed"
            event.next_attempt_at = current_time + RETRY_DELAYS[event.attempt_count - 1]

        session.commit()
        logger.info(
            "Webhook event %s attempt %d: %s (HTTP %s)",
            event_id,
            event.attempt_count,
            event.status,
            http_status,
        )
    return True


def run_worker(
    database_url: str,
    secret: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    from app.database import (
        create_database_engine,
        create_session_factory,
        init_database,
    )

    engine = create_database_engine(database_url)
    init_database(engine)
    session_factory = create_session_factory(engine)
    logger.info("Webhook delivery worker started")
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            while True:
                if not process_one_due_event(session_factory, client, secret):
                    time.sleep(poll_interval_seconds)
    finally:
        engine.dispose()
