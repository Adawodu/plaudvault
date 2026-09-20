"""Filters that must be applied before ranking, and a tier that must gate a provider.

Both are enforcement, not convenience. A date filter that silently does nothing returns
a plausible answer drawn from the wrong months; a tier scope that fails open sends a
therapy session to somebody's API.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from plaudvault import llm
from plaudvault.store import Store

DAY = 86400
T0 = int(time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1)))


def _seed(db):
    st = Store(db)
    for rid, offset, tier in [
        ("mar1", 0, "stack"), ("mar2", 2 * DAY, "local"), ("apr1", 30 * DAY, "stack"),
    ]:
        st.db.execute("INSERT INTO recordings (id, filename, started_at, duration_s) VALUES (?,?,?,?)",
                      (rid, rid, T0 + offset, 600.0))
        if tier:
            st.db.execute("INSERT INTO triage (recording_id, tier, decided_at) VALUES (?,?,?)",
                          (rid, tier, T0))
        st.db.execute("INSERT INTO actions (recording_id, text, kind, owner, status, created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (rid, f"commitment from {rid}", "commitment", "Bayo", "proposed", T0))
    st.db.commit()
    return st


# ------------------------------------------------------------------ actions

def test_a_period_bounds_which_actions_come_back(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    mar = st.actions(since=T0 - DAY, until=T0 + 20 * DAY)
    assert {a["recording_id"] for a in mar} == {"mar1", "mar2"}


def test_the_bound_is_the_recording_not_the_due_date(tmp_path):
    """Only a handful of actions carry a due date. Filtering on that would answer a
    question nobody asked and return almost nothing while looking correct."""
    st = _seed(tmp_path / "m.sqlite")
    st.db.execute("UPDATE actions SET due_at = ? WHERE recording_id = 'apr1'", (T0,))
    st.db.commit()
    got = st.actions(since=T0 - DAY, until=T0 + 20 * DAY)
    assert "apr1" not in {a["recording_id"] for a in got}


def test_kind_and_owner_narrow_further(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    assert len(st.actions(kind="commitment")) == 3
    assert st.actions(kind="suggestion") == []
    assert len(st.actions(owner="bayo")) == 3          # case-insensitive


def test_actions_carry_the_recording_date_for_citation(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    assert all(a["recorded_at"] for a in st.actions())


# ------------------------------------------------------------------ chunks

def test_chunks_are_filtered_by_date_before_ranking(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    for rid in ("mar1", "apr1"):
        st.set_chunks(rid, [{"start_ms": 0, "text": "hello there"}],
                      np.zeros((1, 8), dtype="float32"), model="m")
    got = st.chunks(model="m", since=T0 - DAY, until=T0 + 20 * DAY)
    assert {r["recording_id"] for r in got} == {"mar1"}


def test_chunks_can_be_narrowed_to_a_tier(tmp_path):
    st = _seed(tmp_path / "m.sqlite")
    for rid in ("mar1", "mar2"):
        st.set_chunks(rid, [{"start_ms": 0, "text": "hello"}],
                      np.zeros((1, 8), dtype="float32"), model="m")
    got = st.chunks(model="m", tiers={"stack"})
    assert {r["recording_id"] for r in got} == {"mar1"}


# ------------------------------------------------------- remote provider gate

class _Cfg:
    llm_provider = "openai"
    openai_base_url = "https://api.example.com/v1"
    openai_api_key_env = "KEY"
    cloud_tier_scope = ""

    def openai_api_key(self):
        return "sk-test"


def test_a_key_alone_does_not_open_the_archive():
    """The default is that nothing may leave. Holding credentials and deciding which
    conversations may be sent are two decisions, and one switch for both is how a
    family recording ends up at a vendor."""
    assert llm.remote_allowed(_Cfg(), "stack") is False


def test_only_tiers_in_scope_may_be_sent():
    cfg = _Cfg()
    cfg.cloud_tier_scope = "stack"
    assert llm.remote_allowed(cfg, "stack") is True
    assert llm.remote_allowed(cfg, "local") is False
    assert llm.remote_allowed(cfg, None) is False        # unknown is refused


def test_an_unknown_tier_is_refused_not_assumed_safe():
    cfg = _Cfg()
    cfg.cloud_tier_scope = "stack,local,untriaged"
    assert llm.remote_allowed(cfg, None) is False


def test_generate_refuses_rather_than_falling_back_quietly():
    """Silently downgrading to the local model would produce a different quality of
    output with nothing saying which model wrote it."""
    cfg = _Cfg()
    cfg.cloud_tier_scope = "stack"
    with pytest.raises(llm.RemoteNotPermitted):
        llm.generate(cfg, "hello", tier="local")


def test_a_local_provider_is_never_gated():
    class Local(_Cfg):
        llm_provider = "ollama"
        ollama_host = "http://127.0.0.1:11434"
        ollama_model = "qwen3:latest"
    assert llm.remote_allowed(Local(), None) is True
    assert llm.remote_allowed(Local(), "local") is True


# ------------------------------------------- a local address is not locality

class _Ollama:
    llm_provider = "ollama"
    ollama_host = "http://127.0.0.1:11434"
    ollama_model = "qwen3:latest"
    cloud_tier_scope = ""
    openai_base_url = "http://127.0.0.1:1234/v1"


def test_a_cloud_model_served_through_localhost_is_not_local():
    """Ollama's hosted models are pulled and then addressed at 127.0.0.1 like any
    other — the daemon forwards the prompt to Ollama's servers. An address check alone
    reports "nothing leaves this machine" while a therapy session is in flight."""
    cfg = _Ollama()
    assert llm.is_local(cfg) is True
    cfg.ollama_model = "gpt-oss:120b-cloud"
    assert llm.is_local(cfg) is False


def test_a_cloud_model_is_gated_by_the_tier_scope_like_any_other_provider():
    """The point of fixing is_local: D27's machinery then works unchanged."""
    cfg = _Ollama()
    cfg.ollama_model = "deepseek-v3.1:671b-cloud"
    assert llm.remote_allowed(cfg, "stack") is False      # empty scope: nothing leaves
    cfg.cloud_tier_scope = "stack"
    assert llm.remote_allowed(cfg, "stack") is True
    assert llm.remote_allowed(cfg, "local") is False
    assert llm.remote_allowed(cfg, None) is False


def test_generate_refuses_a_cloud_model_outside_the_scope():
    cfg = _Ollama()
    cfg.ollama_model = "gpt-oss:120b-cloud"
    cfg.cloud_tier_scope = "stack"
    with pytest.raises(llm.RemoteNotPermitted) as exc:
        llm.generate(cfg, "hello", tier="local")
    assert "Ollama's cloud" in str(exc.value)


def test_cloud_detection_needs_a_separator_not_a_substring():
    """Broad enough to catch the naming convention, narrow enough not to refuse a
    local model that merely contains the word."""
    for name in ("gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "qwen3:cloud"):
        assert llm.model_is_cloud(name), name
    for name in ("qwen3:latest", "cloudburst:7b", "nimbuscloud", "", None):
        assert not llm.model_is_cloud(name), name


def test_indexing_refuses_a_hosted_embedding_model():
    """Every other call handles one recording and can be gated by its tier. Indexing
    sends every sentence in the archive in one sweep — there is no tier scope that
    makes that proportionate."""
    from plaudvault import search

    class _Cfg2:
        embed_model = "some-embed:cloud"
        ollama_host = "http://127.0.0.1:11434"

    ok, why = search.available(_Cfg2())
    assert ok is False and "every sentence" in why
    with pytest.raises(search.EmbedError):
        search.embed(_Cfg2(), ["text"])


def test_generation_is_bounded_so_a_repetition_loop_fails_fast():
    """A transcript that is 7% unique words — "thank you" 512 times, from a pin left
    running through a meditation — sends a small model into a repetition loop. Held
    open it burns the full 900s timeout per call; four of those killed a pipeline
    stage. The ceiling turns it into a fast failure."""
    import httpx

    sent = {}

    class _Resp:
        is_success = True
        text = ""

        def json(self):
            return {"response": "ok"}

    def fake_post(url, json=None, **kw):
        sent.update(json or {})
        return _Resp()

    class Cfg(_Ollama):
        llm_num_ctx = 8192
        llm_max_tokens = 4096

    real = httpx.post
    httpx.post = fake_post
    try:
        llm.generate(Cfg(), "hello")
    finally:
        httpx.post = real
    assert sent["options"]["num_predict"] == 4096
    assert sent["options"]["num_ctx"] == 8192
