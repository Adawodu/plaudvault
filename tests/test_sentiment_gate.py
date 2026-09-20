"""Tone scoring must carry the recording's tier all the way to the provider call.

This is where a regression hides well: the tier is read correctly in `run`, and a
segment scored without it still returns a plausible number. The only visible symptom
would be a transcript reaching a remote model that the tier scope forbids — or, as
happened here, every scoring call raising `NameError` inside a broad `except` and being
reported as an ordinary failure.
"""

from __future__ import annotations

import pytest

from plaudvault import sentiment
from plaudvault.llm import RemoteNotPermitted

SAMPLE = "[00:00:00] " + ("we talked about the launch and it went well. " * 30)


def test_the_tier_reaches_the_provider_call(monkeypatch):
    seen = []

    def fake_generate(cfg, prompt, *, tier=None, **kw):
        seen.append(tier)
        return '{"valence": 0.4, "energy": 0.5, "label": "positive", "confidence": 0.8}'

    monkeypatch.setattr("plaudvault.summarize._generate", fake_generate)
    result = sentiment.score_text(object(), SAMPLE, tier="local")
    assert result is not None
    assert seen and set(seen) == {"local"}


def test_an_untriaged_recording_is_scored_with_no_tier_not_a_guess(monkeypatch):
    seen = []
    monkeypatch.setattr("plaudvault.summarize._generate", lambda cfg, p, *, tier=None, **kw: (
        seen.append(tier) or '{"valence": 0, "label": "neutral", "confidence": 0.5}'))
    sentiment.score_text(object(), SAMPLE, tier=None)
    assert seen == [None] * len(seen)


def test_a_refusal_from_the_gate_propagates_rather_than_scoring_anyway(monkeypatch):
    """If the provider gate says no, the honest outcome is no reading at all."""
    def refuse(cfg, prompt, *, tier=None, **kw):
        raise RemoteNotPermitted("tier not in cloud scope")

    monkeypatch.setattr("plaudvault.summarize._generate", refuse)
    with pytest.raises(RemoteNotPermitted):
        sentiment.score_text(object(), SAMPLE, tier="private")


def test_an_unparseable_reading_is_no_reading_not_a_neutral_one(monkeypatch):
    """Filing a parse failure as 'this conversation was neutral' is a lie that then
    shows up on the tone trend as data."""
    monkeypatch.setattr("plaudvault.summarize._generate", lambda cfg, p, *, tier=None, **kw: "not json")
    assert sentiment.score_text(object(), SAMPLE, tier="local") is None
