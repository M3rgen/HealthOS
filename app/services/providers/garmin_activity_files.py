"""Paced and resumable original-file pulls, using the same ownership as scheduled pulls."""

import logging
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import bounded_http
from app.core.logging import event
from app.db.contracts import TransactionSessions
from app.garmin.activity_zip import ENCODING, TOO_LARGE, InvalidActivityFile
from app.garmin.client import (
    GarminClient,
    GarminError,
    GarminRequestError,
    GarminResponseRefused,
)
from app.garmin.exchange import GarminExchange
from app.models.garmin import GarminApiResponse

from .garmin_file_archive import FileOutcome, activity_states, store_file_result

logger = logging.getLogger(__name__)


@dataclass
class FilePullResult:
    requests: int = 0
    downloaded: int = 0
    unavailable: int = 0
    invalid: int = 0
    failed: int = 0
    stopped: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class ActivityFiles:
    def __init__(self, client: GarminClient, sessions: async_sessionmaker, interval: float) -> None:
        self.client, self.sessions, self.interval = client, sessions, interval

    async def run(
        self, owned: TransactionSessions, limit: int, *, dry_run: bool = False
    ) -> FilePullResult:
        report = FilePullResult()
        cursor = 0
        while report.requests < limit:
            states = activity_states()
            async with owned.begin() as session:
                batch = (
                    await session.execute(
                        select(states.c.raw_id, states.c.activity_id)
                        .where(states.c.state == "pending", states.c.raw_id > cursor)
                        .order_by(states.c.raw_id)
                        .limit(min(100, limit - report.requests))
                    )
                ).all()
            if not batch:
                break
            for raw_id, identifier in batch:
                if report.requests and not dry_run:
                    await self.client.sleep(self.interval)
                # Fence immediately before I/O too; no transaction spans a wait or request.
                async with owned.begin():
                    pass
                report.requests += 1
                cursor = raw_id
                if dry_run:
                    continue
                await self._fetch(owned, raw_id, identifier, report)
                if report.stopped:
                    return report
        return report

    async def _fetch(
        self, owned: TransactionSessions, raw_id: int, identifier: str, report: FilePullResult
    ) -> None:
        exchanges: list[GarminExchange] = []
        outcome = await self._download(identifier, exchanges, report)
        # Even a successful retry ends this round after committing the recovered file.
        if any(e.status_code == 429 or e.status_code >= 500 for e in exchanges):
            report.stopped = True
        try:
            async with owned.begin() as session:
                session.add_all(GarminApiResponse(**exchange.row()) for exchange in exchanges)
                if outcome is not None:
                    await store_file_result(
                        session, raw_id, identifier, outcome, self.client.clock()
                    )
        except BaseException:
            # A lost owner can preserve HTTP evidence only, never publish progress.
            async with self.sessions.begin() as session:
                session.add_all(GarminApiResponse(**exchange.row()) for exchange in exchanges)
            raise
        status = outcome.status if outcome is not None else None
        if status == "downloaded":
            report.downloaded += 1
        elif status == "invalid":
            report.invalid += 1
        elif status is not None:
            report.unavailable += 1
        content = outcome.content if outcome is not None else None
        event(
            logger,
            "GARMIN_ACTIVITY_FILE_RESULT",
            downloaded=int(status == "downloaded"),
            unavailable=int(status in ("not_found", "gone")),
            invalid=int(status == "invalid"),
            reason=outcome.reason if outcome is not None else None,
            failed=int(status is None),
            size=len(content) if content is not None else 0,
        )

    async def _download(
        self, identifier: str, exchanges: list[GarminExchange], report: FilePullResult
    ) -> FileOutcome | None:
        """The settled outcome of one request, or None when it must stay pending.

        A received body that is refused (bad envelope, not a FIT, over the byte budget) is
        settled as ``invalid`` for this list version; transport errors, 429/5xx after the
        retries and authentication failures are never invalid and stop the round.
        """
        try:
            content = await self.client.download_activity(identifier, exchanges=exchanges)
            return FileOutcome("downloaded", content)
        except GarminRequestError as exc:
            if exc.status_code in (404, 410):
                return FileOutcome("not_found" if exc.status_code == 404 else "gone")
        except InvalidActivityFile as exc:
            if exchanges:
                received = exchanges[-1]
                return FileOutcome("invalid", None, exc.reason, received.sha256, received.size)
        except GarminResponseRefused as exc:
            if exchanges:
                reason = TOO_LARGE if exc.reason == bounded_http.TOO_LARGE else ENCODING
                return FileOutcome("invalid", None, reason)
        except GarminError:
            pass
        report.failed += 1
        report.stopped = True
        return None
