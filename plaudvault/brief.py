"""A conversation where something got specified, written so an agent can act on it.

The owner's rule for the action board is two or three items, with one exception: a
conversation that specifies something to be built, researched or executed — the sort of
thing handed to an agent. The tempting reading is that such conversations deserve a
larger budget. That is the wrong shape. Sixty checkboxes is not a specification; it is a
specification shredded into sixty pieces, each of which has lost the context that made
it meaningful.

What an agent needs to start work is a **brief**: what is being built and why, the
constraints it must respect, what was already decided, and what is still open. Then a
small number of actions, which selection already produces. So `product` keeps its budget
of three, and gains a document beside it.

**Open questions are the most valuable section and the easiest to lose.** A model asked
to summarise a design conversation will report the decisions and quietly drop the
disagreements, because decisions sound like conclusions and open questions sound like
noise. An agent that acts on the decisions while unaware of the open questions is the
specific failure this is written to prevent, so the section is asked for explicitly and
"none" has to be said out loud rather than shown by an empty heading.

**A brief needs a human-confirmed kind, and that gate was learned the hard way.** The
first real run produced a brief for a recording whose title named two people and a
business topic, classified `product` at 0.95 confidence — because a summary of it
genuinely is business analysis, while the conversation is mostly a personal argument
with some business talk in the middle. A brief is the one artifact here designed to
travel into an agent's context. It is the wrong place to discover a misclassification.

A veto was tried first: let the prompt decline, since this step reads the transcript
while the classifier only read a summary. **It did not fire on that conversation** — a
chunk containing real business analysis has no reason to decline, and no chunk can see
what the conversation is *mostly* about. The veto is kept as a cheap second line, but it
is not the control and must not be trusted as one.

The control is the rule this whole product already runs on: *everything arrives as a
proposal and does nothing until a person accepts it.* An extracted action needs
acceptance before it can be dispatched (D20). A brief is a higher-consequence artifact
than an action, so it obeys the same rule — `source = 'human'` on the conversation's
kind, which is what `plaudctl kinds --set <id> product` records. The model proposes that
a conversation is a specification; a person confirms it; only then is a document written
for an agent to act on.

None of this fixes the real cause. A single file holding a personal argument and a
business discussion is B15, and no classifier reading a summary of it can be right.

**Grounded like everything else.** Each section cites the transcript timestamps it came
from. A brief that cannot be checked against the recording is just a plausible document,
and this archive's whole claim is that you can always go and look.

**A human's edits are never overwritten.** A brief is a working document — the point is
that you correct it and hand it on — so a re-run refuses to clobber one that has been
touched, exactly as a hand-written title survives the titler (D17).
"""

from __future__ import annotations

import time
from pathlib import Path

from . import llm as llm_mod
from . import segment as segment_mod
from .config import Config
from .llm import available
from .store import Store
from .summarize import _chunk, _generate, map_prompts
from .transcribe import read_transcript

# The marker a re-run looks for. Written into the file rather than tracked in the
# database on purpose: the file is the artifact, it gets copied, mailed and pasted into
# an agent's context, and a provenance claim that lives somewhere else is one that stops
# travelling with what it describes.
GENERATED_MARK = "<!-- plaudvault:generated -->"

NOT_A_SPEC = "NOT-A-SPECIFICATION"

PROMPT = """You are writing a brief so that someone — or an agent — can act on what was
specified in this conversation. Not a summary: a working document.

FIRST, check that this is the right kind of conversation. If it is mostly personal — an
argument, a family conversation, a catch-up, therapy, prayer — or if nothing is being
specified, built, researched or planned, then reply with exactly this one line and
nothing else:
""" + NOT_A_SPEC + """

Do that even if some business is discussed in passing. A brief is handed to an agent to
act on, so a conversation that is mostly personal must not become one.

Use only what the transcript supports. Where you state something, cite the timestamp it
came from, like [00:12:30]. If a section has nothing in it, write "None stated" rather
than inventing content or leaving it blank.

Write exactly these sections as markdown headings:

## Intent
What is being built, researched or executed, and why. Two or three sentences.

## Constraints
Anything it must respect: a stack, a deadline, a budget, a regulation, a dependency, a
person who has to approve. One bullet each.

## Decided
What was actually settled in this conversation. One bullet each, each with a timestamp.
A thing someone merely raised is not decided.

## Open
What was raised and NOT resolved — disagreements, unanswered questions, things deferred.
This section matters more than the others: an agent acting on the decisions while
unaware of what is unresolved is the failure this brief exists to prevent. Do not tidy
it away. If genuinely nothing is open, write "None stated".

## Risks
What could go wrong that someone actually named. Not risks you can imagine.

TRANSCRIPT:
{chunk}
"""

MERGE_PROMPT = """These are partial briefs for one conversation, written from
consecutive parts of it. Merge them into a single brief with the same five headings —
## Intent, ## Constraints, ## Decided, ## Open, ## Risks.

Combine duplicates, keep every timestamp, and keep every open question: a later part of
a conversation resolving an earlier question is the only reason to drop one, and if that
happened, move it to ## Decided rather than deleting it.

PARTIAL BRIEFS:
{parts}
"""


def brief_path(cfg: Config, rec_id: str, segment_idx: int = 0) -> Path:
    """One brief per conversation, not per file.

    The segment is in the filename rather than only in the database, so a directory of
    briefs is still legible on its own — these are documents meant to be copied and
    handed on, and a name that needs a database to interpret is a name that stops
    meaning anything the moment it travels.
    """
    return cfg.brief_dir / f"{rec_id}.s{segment_idx}.md"


def was_edited(path: Path) -> bool:
    """Has a person touched this brief since it was generated?

    Absence of the marker means a person wrote or rewrote the file, which is the state
    a re-run must not destroy.
    """
    if not path.exists():
        return False
    return GENERATED_MARK not in path.read_text()


def write_brief(cfg: Config, text: str, *, title: str, when: str,
                tier: str | None = None) -> str:
    """Build the brief for one transcript. One call per chunk, then a merge."""
    raw = map_prompts(cfg, [PROMPT.format(chunk=c) for c in _chunk(text)], tier=tier)
    parts = [p.strip() for p in raw if p and p.strip()]
    # A veto anywhere is a veto. One part of a conversation reading as a specification
    # does not make the conversation one, and the asymmetry is deliberate: writing no
    # brief costs a command, and writing the wrong one costs whatever the agent does
    # with it.
    if any(NOT_A_SPEC in p for p in parts):
        return ""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    body = (
        parts[0]
        if len(parts) == 1
        else _generate(
            cfg,
            MERGE_PROMPT.format(parts="\n\n---\n\n".join(parts)),
            tier=tier,
        )
    )
    return (
        f"# Brief — {title}\n\n"
        f"*{when} · generated from the transcript; every claim should carry a "
        f"timestamp you can check.*\n\n"
        f"{GENERATED_MARK}\n\n{body.strip()}\n"
    )


def run(cfg: Config, store: Store, *, limit: int | None = None,
        force: bool = False, cloud: bool = False) -> dict:
    """Write a brief for every confirmed `product` conversation that lacks one.

    `cloud` routes the writing to `cloud_model`. Unlike selection this does send the
    transcript, so it is gated per recording by `cloud_tier_scope` like any other
    hosted call — a conversation outside that scope raises rather than being written by
    a quietly different model.
    """
    cfg.ensure_dirs()
    ok, why = available(cfg)
    if not ok:
        raise RuntimeError(f"language model unavailable — {why}")
    write_cfg = llm_mod.with_cloud(cfg) if cloud else cfg

    # Confirmed by a person, not merely proposed by the classifier. See the module
    # docstring: this is the same acceptance gate that stands between a proposed action
    # and a dispatched one, applied to a higher-consequence artifact.
    rows = [c for c in store.by_kind("product")
            if (k := store.kind_of(c["recording_id"], c["segment_idx"])) is not None
            and k["source"] == "human"]
    if not force:
        rows = [c for c in rows
                if not brief_path(cfg, c["recording_id"], c["segment_idx"]).exists()]
    if limit:
        rows = rows[:limit]

    stats = {"written": 0, "kept": 0, "declined": 0, "failed": 0,
             "awaiting_confirmation": len(store.by_kind("product")) - len(rows)}
    print(f"  {len(rows)} confirmed product conversations to brief · {write_cfg.llm_label}")
    if cloud:
        print(f"  transcripts will be sent — tiers {sorted(llm_mod.cloud_tiers(cfg)) or 'none'}")

    for i, conv in enumerate(rows, 1):
        row = conv["recording"]
        path = brief_path(cfg, row["id"], conv["segment_idx"])
        if was_edited(path):
            # A brief is meant to be corrected by hand. Overwriting that is the one
            # thing this must never do, even with --force.
            stats["kept"] += 1
            print(f"  [{i}/{len(rows)}] {conv['label'][:56]} — edited by hand, kept")
            continue
        # This conversation's own transcript, read out of the master document.
        text = segment_mod.transcript_for(
            read_transcript(cfg, row["id"]), conv["start_ms"], conv["end_ms"])
        if not text.strip():
            continue
        print(f"  [{i}/{len(rows)}] {conv['label'][:56]} ...", flush=True)
        try:
            t = store.triage_of(row["id"])
            md = write_brief(
                write_cfg, text,
                title=conv["label"],
                when=time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"])),
                tier=(t["tier"] if t else None),
            )
            if not md:
                # Declined or produced nothing — either way there is no brief, and the
                # run says which so a refusal is not read as a failure.
                stats["declined"] += 1
                print("    not a specification conversation — no brief written")
                continue
            path.write_text(md)
            stats["written"] += 1
            print(f"    {path.name}")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats
