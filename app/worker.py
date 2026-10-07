import logging

from app.config import Settings
from app.delivery import run_worker


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_environment()
    run_worker(
        database_url=settings.database_url,
        secret=settings.webhook_secret,
        timeout_seconds=settings.http_timeout_seconds,
        poll_interval_seconds=settings.worker_poll_interval_seconds,
    )


if __name__ == "__main__":
    main()
