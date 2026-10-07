from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker, selectinload

from app.config import Settings
from app.database import (
    create_database_engine,
    create_session_factory,
    init_database,
    session_dependency,
)
from app.models import WebhookEvent, utc_now
from app.schemas import AttemptResponse, EventCreate, EventResponse


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize_event(event: WebhookEvent) -> EventResponse:
    return EventResponse(
        id=event.id,
        type=event.type,
        payload=event.payload,
        webhook_url=event.webhook_url,
        status=event.status,
        created_at=_as_utc(event.created_at),
        attempts=[
            AttemptResponse(
                attempted_at=_as_utc(attempt.attempted_at),
                http_status=attempt.http_status,
                outcome=attempt.outcome,
            )
            for attempt in event.attempts
        ],
    )


def create_app(
    database_url: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_environment()
    engine = create_database_engine(database_url or app_settings.database_url)
    session_factory: sessionmaker[Session] = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_database(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Webhook Delivery Engine", lifespan=lifespan)
    app.state.engine = engine
    app.state.session_factory = session_factory
    get_db = session_dependency(session_factory)

    @app.post(
        "/events",
        response_model=EventResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_event(
        request: EventCreate,
        session: Session = Depends(get_db),
    ) -> EventResponse:
        created_at = utc_now()
        event = WebhookEvent(
            type=request.type,
            payload=request.payload,
            webhook_url=request.webhook_url,
            status="pending",
            created_at=created_at,
            attempt_count=0,
            next_attempt_at=created_at,
        )
        session.add(event)
        session.flush()
        response = _serialize_event(event)
        session.commit()
        return response

    @app.get("/events", response_model=list[EventResponse])
    def list_events(session: Session = Depends(get_db)) -> list[EventResponse]:
        events = session.scalars(
            select(WebhookEvent)
            .options(selectinload(WebhookEvent.attempts))
            .order_by(WebhookEvent.id.desc())
        ).all()
        return [_serialize_event(event) for event in events]

    @app.get("/events/{event_id}", response_model=EventResponse)
    def get_event(
        event_id: int,
        session: Session = Depends(get_db),
    ) -> EventResponse:
        event = session.scalar(
            select(WebhookEvent)
            .options(selectinload(WebhookEvent.attempts))
            .where(WebhookEvent.id == event_id)
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return _serialize_event(event)

    @app.post("/events/{event_id}/retry", response_model=EventResponse)
    def retry_event(
        event_id: int,
        session: Session = Depends(get_db),
    ) -> EventResponse:
        event = session.scalar(
            select(WebhookEvent)
            .options(selectinload(WebhookEvent.attempts))
            .where(WebhookEvent.id == event_id)
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.status != "dead":
            raise HTTPException(
                status_code=400,
                detail="Only dead events can be retried",
            )

        event.status = "pending"
        event.attempt_count = 0
        event.next_attempt_at = utc_now()
        session.commit()
        return _serialize_event(event)

    return app


app = create_app()
