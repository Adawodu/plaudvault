"""Turning "March" into a range, where an off-by-one is invisible.

A wrong range does not error. It returns a shorter list, which reads as "you did not
commit to much in March" — so the properties here are the ones that would quietly
change an answer rather than break a call.
"""

from __future__ import annotations

import time

import pytest

from plaudvault import period

NOW = time.mktime((2026, 9, 3, 12, 0, 0, 0, 0, -1))


def _iso(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None


def test_a_month_name_becomes_that_whole_month():
    a, b = period.parse("March 2026")
    assert (_iso(a), _iso(b)) == ("2026-03-01", "2026-04-01")


def test_the_range_is_half_open_so_adjacent_months_never_overlap():
    _, mar_end = period.parse("March 2026")
    apr_start, _ = period.parse("April 2026")
    assert mar_end == apr_start


def test_a_bare_month_means_the_most_recent_one_that_has_started():
    """Asked for March in September, a future March cannot hold a recording."""
    a, _ = period.parse("March", now=NOW)
    assert _iso(a) == "2026-03-01"


def test_a_month_later_in_the_year_than_today_means_last_year():
    a, _ = period.parse("December", now=NOW)
    assert _iso(a) == "2025-12-01"


def test_december_rolls_the_year_not_the_month():
    a, b = period.parse("December 2026")
    assert (_iso(a), _iso(b)) == ("2026-12-01", "2027-01-01")


def test_a_single_day_is_one_day_long():
    a, b = period.parse("2026-03-14")
    assert (_iso(a), _iso(b)) == ("2026-03-14", "2026-03-15")


def test_empty_means_no_constraint_and_is_not_an_error():
    assert period.parse("") == (None, None)


def test_a_year_covers_the_year():
    a, b = period.parse("2026")
    assert (_iso(a), _iso(b)) == ("2026-01-01", "2027-01-01")


def test_relative_days_leave_the_end_open():
    a, b = period.parse("last 30 days", now=NOW)
    assert b is None and _iso(a) == "2026-08-04"


def test_nonsense_is_refused_rather_than_silently_meaning_all_time():
    """The dangerous failure: an unparsed period treated as no filter would answer a
    question about March with the whole archive and look completely plausible."""
    with pytest.raises(period.BadPeriod):
        period.parse("sometime last spring")
    with pytest.raises(period.BadPeriod):
        period.parse("Marhc 2026")


def test_case_and_spacing_do_not_matter():
    assert period.parse("  MARCH 2026 ") == period.parse("march 2026")
