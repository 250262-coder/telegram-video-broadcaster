"""APScheduler wiring. One interval job, resumed correctly after restarts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db import Database

CycleFunc = Callable[[], Awaitable[object]]

log = logging.getLogger(__name__)

JOB_ID = "broadcast_cycle"
# Grace period on a cold start so you can add videos/groups before the first blast.
COLD_START_DELAY = timedelta(minutes=2)


async def compute_first_run(db: Database, hours: float) -> datetime:
    now = datetime.now(timezone.utc)
    last = await db.last_run_at()
    if last is None:
        return now + COLD_START_DELAY
    due = last + timedelta(hours=hours)
    return due if due > now else now + timedelta(seconds=30)


def install_job(
    scheduler: AsyncIOScheduler,
    cycle: CycleFunc,
    hours: float,
    first_run: datetime | None = None,
) -> None:
    scheduler.add_job(
        cycle,
        trigger=IntervalTrigger(hours=hours),
        id=JOB_ID,
        replace_existing=True,
        next_run_time=first_run,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    log.info("Broadcast job set to every %sh, first run %s", hours, first_run or "on interval")


def reschedule(scheduler: AsyncIOScheduler, cycle: CycleFunc, hours: float) -> datetime:
    first_run = datetime.now(timezone.utc) + timedelta(hours=hours)
    install_job(scheduler, cycle, hours, first_run)
    return first_run


def next_run(scheduler: AsyncIOScheduler) -> datetime | None:
    job = scheduler.get_job(JOB_ID)
    return getattr(job, "next_run_time", None) if job else None
