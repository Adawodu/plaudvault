"""Overlapping the calls, without losing the order or the safety.

Every stage here walks a transcript in chunks and waits for each one in turn. Profiling
said that is the whole runtime: prompt processing is nearly free and generation runs at
about 30 tokens a second, so a five-chunk recording spends five serial waits on a
machine that can overlap them.

Concurrency is the easy part. What these hold down is everything that could quietly go
wrong while doing it — a result landing in the wrong position, a failed chunk taking its
neighbours with it, and a tier refusal being swallowed as though it were a flaky call.
"""

from __future__ import annotations

import threading
import time

import pytest

from plaudvault import sentiment, summarize
from plaudvault.llm import RemoteNotPermitted


class _Cfg:
    llm_workers = 4


def test_results_come_back_in_the_order_they_were_asked_for(monkeypatch):
    """Positions matter: an action's timestamp is read from the chunk it came from, so
    a reply landing one slot over would cite the wrong minute of the recording."""
    def slow(cfg, prompt, **kw):
        # Reverse the natural completion order — the first prompt finishes last.
        time.sleep(0.05 * (5 - int(prompt)))
        return f"reply-{prompt}"

    monkeypatch.setattr(summarize, "_generate", slow)
    got = summarize.map_prompts(_Cfg(), [str(i) for i in range(5)])
    assert got == [f"reply-{i}" for i in range(5)]


def test_the_calls_actually_overlap(monkeypatch):
    """Otherwise this is an elaborate way to write a for loop."""
    live, peak, lock = 0, 0, threading.Lock()

    def tracked(cfg, prompt, **kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return "ok"

    monkeypatch.setattr(summarize, "_generate", tracked)
    summarize.map_prompts(_Cfg(), ["a", "b", "c", "d"])
    assert peak > 1


def test_one_failed_chunk_does_not_take_its_neighbours(monkeypatch):
    """The same isolation every stage already applies per recording, one level down.
    A None marks the gap rather than shifting everything after it."""
    def flaky(cfg, prompt, **kw):
        if prompt == "b":
            raise RuntimeError("model hiccup")
        return f"ok-{prompt}"

    monkeypatch.setattr(summarize, "_generate", flaky)
    got = summarize.map_prompts(_Cfg(), ["a", "b", "c"])
    assert got == ["ok-a", None, "ok-c"]


def test_a_tier_refusal_is_raised_not_swallowed(monkeypatch):
    """The one exception that must not become a None. `RemoteNotPermitted` means this
    recording may not be sent to a remote model; turning it into a few quietly missing
    chunks would let the caller summarise whatever survived as though the archive had
    nothing more to say."""
    def refuse(cfg, prompt, **kw):
        raise RemoteNotPermitted("tier not in cloud scope")

    monkeypatch.setattr(summarize, "_generate", refuse)
    with pytest.raises(RemoteNotPermitted):
        summarize.map_prompts(_Cfg(), ["a", "b", "c"])


def test_a_refusal_wins_even_when_other_chunks_succeed(monkeypatch):
    def mixed(cfg, prompt, **kw):
        if prompt == "b":
            raise RemoteNotPermitted("tier not in cloud scope")
        return "ok"

    monkeypatch.setattr(summarize, "_generate", mixed)
    with pytest.raises(RemoteNotPermitted):
        summarize.map_prompts(_Cfg(), ["a", "b", "c"])


def test_one_worker_is_the_serial_path(monkeypatch):
    """Configurable down to the old behaviour, for a machine that cannot overlap."""
    seen = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda cfg, p, **kw: seen.append(p) or "ok")

    class Serial:
        llm_workers = 1

    assert summarize.map_prompts(Serial(), ["a", "b"]) == ["ok", "ok"]
    assert seen == ["a", "b"]


def test_no_prompts_is_no_calls(monkeypatch):
    monkeypatch.setattr(summarize, "_generate",
                        lambda *a, **k: pytest.fail("should not be called"))
    assert summarize.map_prompts(_Cfg(), []) == []


def test_the_tier_reaches_every_concurrent_call(monkeypatch):
    seen = []
    lock = threading.Lock()

    def note(cfg, prompt, *, tier=None, **kw):
        with lock:
            seen.append(tier)
        return "ok"

    monkeypatch.setattr(summarize, "_generate", note)
    summarize.map_prompts(_Cfg(), ["a", "b", "c"], tier="local")
    assert seen == ["local"] * 3


# ------------------------------------------------------- sampling for tone

def test_a_long_recording_is_sampled_not_read_whole():
    """Forty model calls to move one number was the largest block of calls in a pass,
    for the least detailed output in the archive."""
    got = sentiment._sample([f"c{i}" for i in range(40)])
    assert len(got) == sentiment.MAX_SEGMENTS


def test_the_sample_keeps_both_ends():
    """A conversation's opening and close carry most of what a tone reading is asked
    about — how it started, how it ended, and whether those differ."""
    positions = [i for i, _ in sentiment._sample([f"c{i}" for i in range(40)])]
    assert positions[0] == 1 and positions[-1] == 40


def test_the_sample_is_spread_rather_than_clustered():
    positions = [i for i, _ in sentiment._sample([f"c{i}" for i in range(40)])]
    gaps = [b - a for a, b in zip(positions[:-1], positions[1:], strict=True)]
    assert max(gaps) - min(gaps) <= 2


def test_a_short_recording_is_read_in_full():
    got = sentiment._sample(["a", "b", "c"])
    assert [i for i, _ in got] == [1, 2, 3]


def test_the_sample_carries_its_positions():
    """A sampled reading that could not say where it looked would be a number with no
    way to check it."""
    got = sentiment._sample([f"c{i}" for i in range(40)])
    assert all(isinstance(i, int) and i >= 1 for i, _ in got)
    assert [c for _, c in got][0] == "c0"


def test_local_runs_serially_because_bandwidth_not_latency_is_the_wait():
    """Measured, not assumed: 65.8s serial against 66.9s with two workers on a real
    five-chunk transcript with the model warm. One stream of an 8B at Q4 already reads
    156 GB/s of an M4 Pro's 273 GB/s, so a second has nowhere to run."""
    from plaudvault.config import load

    assert load().llm_workers == 1


def test_reaching_for_the_cloud_raises_the_worker_count():
    """The case concurrency was built for: a hosted call waits on the network and on a
    provider running its own parallelism, neither of which is this machine's memory
    bus."""
    from dataclasses import replace

    from plaudvault import llm
    from plaudvault.config import load

    cfg = replace(load(), cloud_model="glm-5.3:cloud", llm_workers=1)
    assert llm.with_cloud(cfg).llm_workers >= 4
