"""A quote that cannot be traced back to the transcript is worse than a wrong action.

The quote is the thing you would check the action against, so a fabricated one defeats
its own audit. D9 settled how strict this gets: verbatim matching would have discarded
23 sound actions to catch 2 bad ones, so the rule is a 40-character run or most of the
content words. These tests pin both ends of that trade — the leaked few-shot example
must fail, and a legitimate paraphrase must pass.
"""

from __future__ import annotations

from plaudvault.extract import _grounded, _norm

TRANSCRIPT = _norm(
    "[00:04:12] Right, so on the Metric side I'll pull the scoring thresholds together "
    "and get them over to the clinical lead before the board call on Thursday. "
    "[00:05:01] And we still owe them the reimbursement memo, which I haven't started."
)


def test_a_verbatim_quote_passes():
    assert _grounded("I'll pull the scoring thresholds together", TRANSCRIPT)


def test_a_paraphrase_that_keeps_the_content_words_passes():
    """Models legitimately elide and reword. Refusing that is D9's rejected option."""
    assert _grounded("I will pull the scoring thresholds together and send them "
                     "to the clinical lead", TRANSCRIPT)


def test_the_few_shot_example_does_not_survive():
    """The one thing in the prompt that looks exactly like a correct answer. A small
    model returns it instead of reading, citing a conversation that never happened."""
    assert not _grounded(
        "I'll draft the vendor agreement by Friday and send it over", TRANSCRIPT)


def test_an_invented_quote_about_the_right_topic_is_still_refused():
    assert not _grounded(
        "We should probably rebuild the entire scheduling platform in Rust", TRANSCRIPT)


def test_a_quote_too_short_to_verify_is_not_treated_as_proof():
    """Under 12 normalised characters there is nothing to match against, so this
    passes here and is judged elsewhere — it must not silently mean 'verified'."""
    assert _grounded("ok", TRANSCRIPT)
    assert _grounded("", TRANSCRIPT)


def test_stop_words_alone_cannot_ground_a_quote():
    """'I need to have this done' shares every word with any transcript in English."""
    assert not _grounded(
        "I think we need to have that done and so I will be going to do it for them",
        _norm("Completely unrelated conversation about the wedding photographs."))


def test_punctuation_and_case_do_not_decide_it():
    assert _grounded("ILL PULL THE SCORING THRESHOLDS, TOGETHER!!", TRANSCRIPT)
