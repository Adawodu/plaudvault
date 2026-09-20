"""Pull commitments out of a transcript, locally, as *proposals*.

Everything this produces lands in status `proposed` and is worthless until a human
accepts it in the console. That's deliberate: a local model reading noisy ASR of a
family conversation will confidently invent obligations, so extraction is a
suggestion engine, not an authority. Precision is favoured over recall — a missed
action costs you a scroll through the transcript; a fabricated one costs trust in
the whole board.
"""

from __future__ import annotations

import json
import re
import time

from . import kinds as kinds_mod
from . import llm as llm_mod
from . import segment as segment_mod
from . import select as select_mod
from .config import Config
from .llm import available
from .store import Store
from .summarize import _chunk, _generate, summary_path
from .transcribe import read_transcript

# Two prompts rather than one prompt plus a filter. Asking for suggestions and then
# discarding them still spends the model's attention on inventing them, and a category
# that is merely *mentioned* is a category the model will populate. When suggestions are
# off, the concept does not appear anywhere in the prompt — not in the rules, not in the
# schema, not in the worked example.
_RULES_COMMITMENTS_ONLY = """One thing counts, and only one:
- "commitment" — a person actually said they would do it ("I'll send it Friday",
  "I need to email Herry", "let me sort that out tomorrow")

This is a strict test and most of a conversation fails it. Do NOT list something
because it sounds useful, because it was discussed at length, because it is a problem
worth solving, or because the conversation implies it would be a good next step. If
nobody said they would do it, it does not go on the list. Topics are not commitments.
Ideas are not commitments. Things that already happened during the conversation are not
commitments."""

_RULES_WITH_SUGGESTIONS = """Two kinds count:
- "commitment" — someone said they would do it ("I'll send it Friday", "I need to email Herry")
- "suggestion" — the discussion clearly implies a useful next step, even if nobody
  explicitly committed to it"""

_KIND_FIELD_COMMITMENTS_ONLY = '"kind": "commitment",'
_KIND_FIELD_WITH_SUGGESTIONS = '"kind": "commitment" or "suggestion",'

_EXAMPLE_COMMITMENTS_ONLY = """[{{"text":"Draft the vendor agreement and send it over","kind":"commitment","owner":"Speaker 1","quote":"I'll draft the vendor agreement by Friday and send it over","at":"00:01:10"}}]

Note what was skipped. The weather remark is small talk. The scattered renewal dates are
a real problem and consolidating them would obviously help — but nobody said they would
do it, so it is not a commitment and does not appear."""

_EXAMPLE_WITH_SUGGESTIONS = """[{{"text":"Draft the vendor agreement and send it over","kind":"commitment","owner":"Speaker 1","quote":"I'll draft the vendor agreement by Friday and send it over","at":"00:01:10"}},
 {{"text":"Consolidate the renewal dates into one source","kind":"suggestion","owner":"","quote":"The renewal dates are scattered across three spreadsheets","at":"00:02:40"}}]

Note what was skipped: the weather remark is small talk, so it produced nothing."""

EXTRACT_PROMPT = """Read this conversation transcript and list the things that should end up
on a to-do list.

{rules}

Return a JSON array. Each element:
{{{{"text": "the action, imperative, one line",
  {kind_field}
  "owner": "who would do it, or empty string if unclear",
  "quote": "the transcript line it came from, copied exactly",
  "at": "the [HH:MM:SS] timestamp of that line"}}}}

Here is a worked example.

TRANSCRIPT:
[00:01:10] Okay, on the billing work, I'll draft the vendor agreement by Friday and send it over.
[00:02:05] Yeah, the weather has been strange lately.
[00:02:40] The renewal dates are scattered across three spreadsheets, it's a mess.

OUTPUT:
{example}

Guidance: skip small talk and pleasantries. This transcript comes from automatic speech
recognition, so if a line is too garbled to understand, skip it rather than guessing at
it. Every "quote" must be copied from the transcript below — never from this example.
Plenty of conversations contain nothing actionable — if this is one of them, return an
empty array [] rather than manufacturing something.

Return AT MOST {max_items} items — the strongest ones. Most passages hold one or two real
commitments and many hold none, so a short array is the usual answer and an empty one is
a correct answer.

Return ONLY the JSON array, no prose and no code fences.

TRANSCRIPT:
{{chunk}}

OUTPUT:
"""


# A ceiling on what one chunk may return. It is a performance fix and a precision fix
# in the same line, which is unusual enough to be worth explaining.
#
# Measured on a real 12,000-character chunk: uncapped, the model returned 55 items and
# spent 2,400 output tokens doing it. Generation runs at ~30 tokens/sec on this machine
# and prompt processing is nearly free — 5,634 prompt tokens cost under a second — so
# the entire cost of extraction is the length of what it writes. Asked for at most 8, the
# same chunk took 34s instead of 138s and returned 8 usable items instead of a truncated
# array that parsed to nothing.
#
# Nothing downstream wanted 55. A conversation's budget is 3, selection sees every
# candidate at once, and a recording of five chunks still offers 40 for those 3 places.
# Asking for fewer is not lowering recall; it is declining to pay for candidates that
# exist only to be discarded.
MAX_PER_CHUNK = 8


def build_prompt(*, suggestions: bool, max_items: int = MAX_PER_CHUNK) -> str:
    return EXTRACT_PROMPT.format(
        max_items=max_items,
        rules=_RULES_WITH_SUGGESTIONS if suggestions else _RULES_COMMITMENTS_ONLY,
        kind_field=_KIND_FIELD_WITH_SUGGESTIONS if suggestions else _KIND_FIELD_COMMITMENTS_ONLY,
        example=_EXAMPLE_WITH_SUGGESTIONS if suggestions else _EXAMPLE_COMMITMENTS_ONLY,
    )

# Brackets optional: the model echoes "[00:01:10]" from the transcript in `quote`,
# but returns a bare "00:01:10" in `at`.
TS_RE = re.compile(r"\[?(\d{1,2}):(\d{2}):(\d{2})\]?")


def _parse_ts(text: str) -> int | None:
    m = TS_RE.search(text or "")
    if not m:
        return None
    h, mnt, s = (int(g) for g in m.groups())
    return (h * 3600 + mnt * 60 + s) * 1000


def _parse_json_array(raw: str) -> list[dict]:
    """Read the model's array, recovering what survives a cut-off.

    A reply that ran into the output ceiling ends mid-object, with no closing bracket.
    Returning [] for that is indistinguishable from "this passage held nothing", which
    is a real and common answer — so a whole chunk's worth of commitments could vanish
    and the run would report a normal, quiet zero. It happened: a chunk that genuinely
    held items hit the ceiling and parsed to nothing.

    So a truncated array is salvaged element by element rather than discarded. The
    complete objects before the cut are real extractions and are kept; the partial one
    at the end is dropped.
    """
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start = raw.find("[")
    if start < 0:
        return []
    end = raw.rfind("]")
    if end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return [d for d in data
                    if isinstance(d, dict) and (d.get("text") or "").strip()]
        except json.JSONDecodeError:
            pass
    return _salvage_objects(raw[start:])


def _salvage_objects(body: str) -> list[dict]:
    """Every complete {...} in a possibly-truncated array, in order."""
    out, depth, obj_start, in_str, escaped = [], 0, None, False, False
    for i, ch in enumerate(body):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    d = json.loads(body[obj_start : i + 1])
                except json.JSONDecodeError:
                    d = None
                if isinstance(d, dict) and (d.get("text") or "").strip():
                    out.append(d)
                obj_start = None
    return out


# Words too common to prove anything about where a quote came from.
_STOP = frozenset(
    "the a an and or of to in for on is are was were be been i you he she it we they "
    "that this with as at by from need needs going gonna so like just have has had do "
    "does did will would can could my your our their".split()
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def _grounded(quote: str, chunk_norm: str) -> bool:
    """Can this quote be traced back to the text the model was actually given?

    A few-shot example is the one thing in the prompt that looks exactly like a
    correct answer, and a small model will occasionally return it instead of reading
    the transcript — producing an action with a citation to a conversation that never
    happened. That is worse than a wrong action: the quote is the thing you would
    check it against, so a fabricated quote defeats its own audit.

    Verbatim matching is too strict, because models legitimately elide and reword
    ("...", "[have them]"), and on real data that would have discarded 23 sound
    actions to catch 2 bad ones. So a quote passes if a 40-character run of it appears
    verbatim, or if most of its content words do. Both leaked examples fail; every
    genuine paraphrase in the corpus passes.
    """
    q = _norm(quote)
    if len(q) < 12:
        return True  # nothing to verify against; the text itself is judged elsewhere
    if q in chunk_norm:
        return True
    if any(q[i : i + 40] in chunk_norm for i in range(0, max(1, len(q) - 40), 10)):
        return True
    words = [w for w in q.split() if w not in _STOP and len(w) > 3]
    if not words:
        return True
    return sum(w in chunk_norm for w in words) / len(words) >= 0.6


def extract_from_text(cfg: Config, text: str, *, suggestions: bool | None = None,
                      tier: str | None = None) -> list[dict]:
    if suggestions is None:
        suggestions = cfg.extract_suggestions
    prompt = build_prompt(suggestions=suggestions)
    found: list[dict] = []
    dropped = skipped_kind = 0
    for chunk in _chunk(text):
        raw = _generate(cfg, prompt.format(chunk=chunk), tier=tier)
        chunk_norm = _norm(chunk)
        for item in _parse_json_array(raw):
            if not _grounded(str(item.get("quote") or ""), chunk_norm):
                dropped += 1
                continue
            kind = str(item.get("kind") or "commitment").strip().lower()
            if kind not in ("commitment", "suggestion"):
                kind = "commitment"
            # Backstop. The prompt never mentions suggestions when they are off, but a
            # model that volunteers one anyway must not slip onto the board sideways.
            if kind == "suggestion" and not suggestions:
                skipped_kind += 1
                continue
            found.append(
                {
                    "text": str(item["text"]).strip()[:500],
                    "kind": kind,
                    "owner": str(item.get("owner") or "").strip()[:100],
                    "quote": str(item.get("quote") or "").strip()[:500],
                    "at_ms": _parse_ts(str(item.get("at") or "")) or _parse_ts(str(item.get("quote") or "")),
                }
            )

    # Near-duplicate collapse across chunk boundaries.
    seen: set[str] = set()
    unique = []
    for f in found:
        key = re.sub(r"[^a-z0-9 ]", "", f["text"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    # Never silent: a filter you can't see is one you stop trusting.
    if dropped:
        print(f"    [dropped {dropped} with a quote not traceable to the transcript]")
    if skipped_kind:
        print(f"    [dropped {skipped_kind} suggestion(s) — commitments only; --suggestions to keep]")
    return unique


def run(
    cfg: Config,
    store: Store,
    *,
    limit: int | None = None,
    force: bool = False,
    suggestions: bool | None = None,
    cloud_select: bool = False,
) -> dict:
    """Extract locally; optionally *choose* with a larger model.

    The split is deliberate. Extraction is ~15 calls per recording and wants recall,
    which a local 8B does adequately. Selection is one call per recording and is pure
    judgement, which is where a large model is worth what it costs — and where sending
    less text buys more. `cloud_select` sends only the candidate list and the summary,
    never the transcript.
    """
    # The working view: one conversation per row, so a file holding four of them is
    # scanned four times with four budgets rather than once with one.
    rows = [c for c in store.conversations()
            if force or not c["recording"]["extracted_at"]]
    if limit:
        rows = rows[:limit]

    stats = {"recordings": 0, "proposed": 0, "failed": 0, "not_expected": 0,
             "unclassified": 0, "overflow": 0, "undecided": 0}
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")
    select_cfg = llm_mod.with_cloud(cfg) if cloud_select else cfg
    want = cfg.extract_suggestions if suggestions is None else suggestions
    scope = "commitments and suggestions" if want else "commitments"
    print(f"  {len(rows)} conversations to scan for {scope} · {cfg.llm_label}")
    if cloud_select:
        print(f"  choosing with {select_cfg.llm_label} — candidates and summary only, "
              f"tiers {sorted(llm_mod.cloud_tiers(cfg)) or 'none'}")

    # `extracted_at` is a clock on the RECORDING, and extraction is now per
    # conversation. Stamping it when the first of four segments finishes would mark the
    # whole file done and silently skip the other three on the next run — so the clock
    # is set only once every segment of that recording has been handled in this pass.
    handled: dict[str, set[int]] = {}
    expected = {r["recording_id"]: len(store.segments(r["recording_id"])) for r in rows}

    def _mark(conv: dict) -> None:
        done = handled.setdefault(conv["recording_id"], set())
        done.add(conv["segment_idx"])
        if len(done) >= expected.get(conv["recording_id"], 1):
            store.update(conv["recording_id"], extracted_at=int(time.time()))

    for i, conv in enumerate(rows, 1):
        row = conv["recording"]
        # The transcript of *this conversation*, read out of the master. Nothing on
        # disk is cut: an unsegmented recording returns the whole document.
        text = segment_mod.transcript_for(
            read_transcript(cfg, row["id"]), conv["start_ms"], conv["end_ms"])
        if not text.strip():
            continue
        # What kind of conversation this is decides whether to ask at all. Asking a
        # played-back podcast "what commitments are here?" fifteen times, once per
        # chunk, is how one recording produced 69 action items.
        k = store.kind_of(row["id"], conv["segment_idx"])
        kind = k["kind"] if k else None
        if k is None:
            # Not classified is not the same as no actions expected. Extracting
            # unbudgeted is the old behaviour, and saying so is cheaper than a silent
            # difference between two recordings on the same board.
            stats["unclassified"] += 1
        elif not kinds_mod.extractable(kind):
            _mark(conv)
            stats["not_expected"] += 1
            print(f"  [{i}/{len(rows)}] {conv['label'][:60]} — {kind}, "
                  f"no actions expected")
            continue
        print(f"  [{i}/{len(rows)}] {conv['label'][:60]} ...", flush=True)
        try:
            existing = {
                re.sub(r"[^a-z0-9 ]", "", a["text"].lower())[:60]
                for a in store.actions(recording_id=row["id"],
                                       segment_idx=conv["segment_idx"])
            }
            _t = store.triage_of(row['id'])
            tier = _t['tier'] if _t else None
            fresh = [
                item for item in extract_from_text(cfg, text, suggestions=suggestions,
                                                   tier=tier)
                if re.sub(r"[^a-z0-9 ]", "", item["text"].lower())[:60] not in existing
            ]

            # Recall first, precision second. Extraction is asked once per chunk and
            # over-produces by design; this is the one call per recording that spends
            # the budget, and it can see every candidate at once.
            sp = summary_path(cfg, row["id"])
            # A segment has no summary of its own, so it is described by its own
            # opening rather than by a summary of the whole file — which, for a
            # recording holding four conversations, describes none of them.
            context = (sp.read_text() if sp.exists() and not conv["segmented"]
                       else text[:2500])
            chosen = select_mod.choose(
                select_cfg, fresh,
                summary=context,
                kind=kind or "other",
                budget=kinds_mod.budget(kind),
                tier=tier,
            )
            for item in chosen["kept"]:
                store.add_action(recording_id=row["id"],
                                 segment_idx=conv["segment_idx"], **item)
            for item in chosen["overflow"]:
                store.add_action(recording_id=row["id"], status="overflow",
                                 segment_idx=conv["segment_idx"], **item)

            _mark(conv)
            stats["recordings"] += 1
            stats["proposed"] += len(chosen["kept"])
            stats["overflow"] += len(chosen["overflow"])
            if not chosen["decided"]:
                stats["undecided"] += 1
            # Both numbers, always. The count below the line is the claim this step
            # makes about its own judgement, and hiding it would make the board look
            # like the whole of what was found.
            below = (f", {len(chosen['overflow'])} below the line"
                     if chosen["overflow"] else "")
            undecided = "" if chosen["decided"] else " [selection failed — kept all]"
            print(f"    {len(chosen['kept'])} proposed{below}{undecided}")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    if stats["not_expected"]:
        print(f"  [{stats['not_expected']} skipped — a kind that yields no actions]")
    if stats["unclassified"]:
        print(f"  [{stats['unclassified']} had no kind yet — run: plaudctl kinds]")
    if stats["undecided"]:
        print(f"  [{stats['undecided']} kept in full — selection returned nothing usable]")
    return stats
