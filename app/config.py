import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    webhook_secret: str
    http_timeout_seconds: float
    worker_poll_interval_seconds: float

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./webhook.db"),
            webhook_secret=os.getenv(
                "WEBHOOK_SECRET", "development-only-change-me"
            ),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
            worker_poll_interval_seconds=float(
                os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1")
            ),
        )
