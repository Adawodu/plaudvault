"""What extraction asks for decides what the pipeline costs.

Measured on this machine: prompt processing is nearly free — 5,634 prompt tokens cost
under a second — while generation runs at about 30 tokens a second. So the entire cost
of a call is the length of what the model writes, and an uncapped extraction wrote 2,400
tokens of 55 candidates for a conversation whose budget is 3.

These pin the two fixes that came out of that: ask for fewer, and never read a cut-off
reply as an empty one.
"""

from __future__ import annotations

from plaudvault import extract

# --------------------------------------------------------------- the ceiling

def test_the_prompt_asks_for_a_bounded_number():
    p = extract.build_prompt(suggestions=False)
    assert f"AT MOST {extract.MAX_PER_CHUNK} items" in p


def test_the_ceiling_still_permits_nothing():
    """A short array is the usual answer and an empty one is correct. A cap that reads
    as a quota would turn a leading question into a louder one."""
    p = extract.build_prompt(suggestions=False).lower()
    assert "empty one is a correct answer" in p or "empty array []" in p


def test_the_ceiling_is_adjustable_without_editing_the_prompt():
    assert "AT MOST 3 items" in extract.build_prompt(suggestions=False, max_items=3)


def test_a_chunk_still_offers_far_more_than_a_budget_can_take():
    """Not a recall cut. A five-chunk recording offers 40 candidates for 3 places, and
    selection sees all of them at once."""
    assert extract.MAX_PER_CHUNK * 5 > 10 * 3


# ------------------------------------------------------- reading a cut reply

def test_a_complete_array_is_read():
    got = extract._parse_json_array(
        '[{"text":"Send the deck","quote":"I\'ll send it"},'
        ' {"text":"Book the room","quote":"I\'ll book it"}]')
    assert [g["text"] for g in got] == ["Send the deck", "Book the room"]


def test_a_truncated_array_keeps_what_survived():
    """The failure this was written for: a reply that ran into the output ceiling ends
    mid-object with no closing bracket. Returning [] is indistinguishable from "this
    passage held nothing", so a whole chunk's commitments vanish into a quiet zero."""
    got = extract._parse_json_array(
        '[{"text":"Send the deck","quote":"I\'ll send it"},'
        ' {"text":"Book the room","quote":"I\'ll book it"},'
        ' {"text":"Call the lawyer","quo')
    assert [g["text"] for g in got] == ["Send the deck", "Book the room"]


def test_an_empty_array_is_still_empty():
    """Salvage must not invent items where the model correctly found none."""
    assert extract._parse_json_array("[]") == []
    assert extract._parse_json_array("[ ]") == []


def test_prose_with_no_array_is_nothing():
    assert extract._parse_json_array("I could not find any commitments.") == []


def test_braces_inside_a_quote_do_not_confuse_the_salvage():
    got = extract._parse_json_array(
        '[{"text":"Use {curly} braces","quote":"say {this}"},{"text":"x","quo')
    assert len(got) == 1 and got[0]["text"] == "Use {curly} braces"


def test_an_escaped_quote_inside_a_string_does_not_end_it():
    got = extract._parse_json_array(
        r'[{"text":"He said \"go\" then left","quote":"q"},{"text":"y","quo')
    assert len(got) == 1 and "go" in got[0]["text"]


def test_a_fenced_reply_is_still_read():
    got = extract._parse_json_array('```json\n[{"text":"a","quote":"q"}]\n```')
    assert len(got) == 1


def test_an_item_with_no_text_is_dropped():
    got = extract._parse_json_array('[{"text":"","quote":"q"},{"text":"real","quote":"q"}]')
    assert [g["text"] for g in got] == ["real"]
