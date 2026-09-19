"""Turning what a person says about time into a half-open range of epoch seconds.

An agent relaying "commitments in March" should not have to know that March 2026 began
at a particular Unix second, and a tool that only accepts `YYYY-MM-DD` pushes that
conversion onto the caller — where an off-by-one-month error looks exactly like an
archive with nothing in it.

Ranges are **half-open**: `[start, end)`. March ends the instant April begins, so a
recording at 23:59:59 on the 31st is inside March and nothing is double-counted by two
adjacent queries.

Everything is local time, because the question "what did I say in March" is asked about
the months a person lived through, not about UTC.
"""

from __future__ import annotations

import calendar
import re
import time

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


class BadPeriod(ValueError):
    """The string did not name a period. Carries what forms are accepted."""


def _epoch(y: int, mo: int, d: int) -> int:
    return int(time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1)))


def _month_range(year: int, month: int) -> tuple[int, int]:
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return _epoch(year, month, 1), _epoch(nxt[0], nxt[1], 1)


def parse(text: str, *, now: float | None = None) -> tuple[int | None, int | None]:
    """`"March"`, `"March 2026"`, `"2026-03"`, `"2026-03-14"`, `"last 30 days"`, `""`.

    Returns `(since, until)` in epoch seconds, either of which may be None for an
    open end. An empty string means no constraint at all, which is not an error — it
    is how a caller says "everywhere".

    A bare month name resolves to the most recent one that has already *started*, so
    asking for "March" in September means this year's March rather than a March six
    months in the future that cannot contain a recording.
    """
    s = (text or "").strip().lower()
    if not s:
        return None, None
    t = time.localtime(now if now is not None else time.time())

    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        y, mo, d = (int(g) for g in m.groups())
        start = _epoch(y, mo, d)
        return start, start + 86400

    if m := re.fullmatch(r"(\d{4})-(\d{2})", s):
        return _month_range(int(m.group(1)), int(m.group(2)))

    if m := re.fullmatch(r"(\d{4})", s):
        return _epoch(int(s), 1, 1), _epoch(int(s) + 1, 1, 1)

    if m := re.fullmatch(r"last\s+(\d{1,4})\s+days?", s):
        end = int(time.mktime(t))
        return end - int(m.group(1)) * 86400, None

    if s in ("today", "yesterday"):
        midnight = _epoch(t.tm_year, t.tm_mon, t.tm_mday)
        off = 0 if s == "today" else 86400
        return midnight - off, midnight - off + 86400

    if m := re.fullmatch(r"([a-z]+)\s*(\d{4})?", s):
        name, year = m.group(1), m.group(2)
        if name in MONTHS:
            mo = MONTHS[name]
            if year:
                return _month_range(int(year), mo)
            # No year given: the most recent occurrence that has begun.
            y = t.tm_year if mo <= t.tm_mon else t.tm_year - 1
            return _month_range(y, mo)

    raise BadPeriod(
        "expected a month (\"March\", \"March 2026\"), a date (2026-03-14), "
        "a month (2026-03), a year (2026), \"last 30 days\", \"today\", or empty for all time"
    )
