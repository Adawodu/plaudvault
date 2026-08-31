"""plaudctl — own your Plaud recordings end to end."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (auth, diarize, dispatch, extract, freshness, notes, prune,
               runlock, search, sentiment, service, setup_wizard, story,
               summarize, sync, tiering, titles, transcribe)
from .api import PlaudClient
from .config import ArchiveUnavailable, load
from .store import Store


def _client(cfg):
    return PlaudClient(auth.require_token(cfg), cfg.api_base)


def cmd_login(args, cfg) -> int:
    auth.login(cfg, args.email)
    cfg = load()
    with _client(cfg) as client:
        devices = client.devices()
    print(f"  connected — {len(devices)} device(s):")
    for d in devices:
        print(f"    {d.get('name')} ({d.get('model')}) sn={d.get('sn')} fw={d.get('version_number')}")
    return 0


def cmd_logout(args, cfg) -> int:
    if cfg.email and auth.keychain_delete(cfg.keychain_service, cfg.email):
        print("  token removed from Keychain")
    else:
        print("  no stored token")
    return 0


def cmd_sync(args, cfg) -> int:
    with _client(cfg) as client, Store(cfg.db_path) as store:
        s = sync.sync(cfg, client, store, limit=args.limit)
    print(
        f"\n  downloaded {s['new']} ({s['bytes'] / 1e9:.2f} GB), skipped {s['skipped']}, "
        f"failed {s['failed']}, unverified {s['unverified']}"
    )
    return 1 if s["failed"] else 0


def cmd_verify(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = sync.verify(cfg, store)
    print(f"\n  ok {s['ok']}, missing {s['missing']}, corrupt {s['corrupt']}")
    return 1 if (s["missing"] or s["corrupt"]) else 0


def cmd_transcribe(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = transcribe.run(cfg, store, limit=args.limit, force=args.force)
    if s["wall_seconds"]:
        print(
            f"\n  transcribed {s['done']}, failed {s['failed']} — "
            f"{s['audio_seconds'] / 3600:.1f}h audio in {s['wall_seconds'] / 60:.0f}m wall "
            f"({s['audio_seconds'] / s['wall_seconds']:.0f}x realtime)"
        )
    return 1 if s["failed"] else 0


def cmd_summarize(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = summarize.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  summarized {s['done']}, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_sentiment(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = sentiment.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  scored {s['scored']}, skipped {s['skipped']} (too short), failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_notes(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = notes.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  wrote {s['written']} notes, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_extract(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = extract.run(cfg, store, limit=args.limit, force=args.force,
                        suggestions=True if args.suggestions else None)
    print(f"\n  scanned {s['recordings']}, proposed {s['proposed']} actions, failed {s['failed']}")
    print("  review them in the console: plaudctl web")
    return 1 if s["failed"] else 0


def cmd_title(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = titles.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  titled {s['titled']}, left unnamed {s['unnamed']}, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_diarize(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = diarize.run(cfg, store, limit=args.limit, force=args.force,
                        num_speakers=args.speakers)
    print(f"\n  diarized {s['done']}, failed {s['failed']} — "
          f"{s['labels']} voices, {s['matched']} recognised from voiceprints")
    if s["done"]:
        print("  name the unknown ones: plaudctl speakers unknown")
    return 1 if s["failed"] else 0


def cmd_speakers(args, cfg) -> int:
    """The identity side of diarization: who these voices belong to."""
    action = args.action

    if action == "login":
        # A secret must not arrive as an argv token — that puts it in shell history
        # and in every `ps` listing on the machine. So it comes from a hidden prompt
        # when there is a terminal, and from stdin when there is not: a piped token
        # covers scripts, CI, and the no-TTY shells embedded in editors and agents,
        # where getpass raises rather than prompting.
        import getpass

        if sys.stdin.isatty():
            try:
                token = getpass.getpass("  HuggingFace token (hf_...): ").strip()
            except (EOFError, OSError):
                token = ""
        else:
            token = sys.stdin.readline().strip()
            if not token:
                print("error: no terminal to prompt on, and nothing on stdin.",
                      file=sys.stderr)
                print("  pipe the token in:  echo hf_xxx | plaudctl speakers login",
                      file=sys.stderr)
                print(f"  or set it for one run:  export {cfg.hf_token_env}=hf_xxx",
                      file=sys.stderr)
                return 2
        if not token:
            print("  nothing entered")
            return 1
        if not token.startswith("hf_"):
            # Caught here rather than 40 minutes into a diarization run that 401s.
            print(f"error: that does not look like a HuggingFace token "
                  f"(expected it to start with 'hf_', got {token[:4]!r}…)", file=sys.stderr)
            return 2
        auth.keychain_set(cfg.keychain_service, "huggingface", token)
        print(f"  stored in the OS keyring (service={cfg.keychain_service})")
        print("  the diarization models are gated — accept the licence at:")
        for m in diarize.GATED_MODELS:
            print(f"    https://hf.co/{m}")
        return 0

    if action == "status":
        ok, why = diarize.status(cfg)
        print(f"  diarization: {'ok ' if ok else 'XX '}{why}")
        if not ok:
            print("  the models are free but gated — accept the licence at:")
            for m in diarize.GATED_MODELS:
                print(f"    https://hf.co/{m}")
            print(f"  then: plaudctl speakers login   (or export {cfg.hf_token_env}=...)")
        with Store(cfg.db_path) as store:
            n_diar = store.db.execute(
                "SELECT COUNT(*) FROM recordings WHERE diarized_at IS NOT NULL"
            ).fetchone()[0]
            print(f"  {n_diar} recordings diarized, {len(store.unnamed_labels())} voices unnamed")
            print(f"  match threshold: {cfg.speaker_match_threshold} cosine")
        return 0

    with Store(cfg.db_path) as store:
        if action == "list":
            rows = store.speakers()
            if not rows:
                print("  nobody named yet — run: plaudctl speakers unknown")
                return 0
            for sp in rows:
                me = " (you)" if sp["is_me"] else ""
                vp = f"voiceprint from {sp['voiceprint_n']}" if sp["voiceprint"] else "no voiceprint"
                ref = f" · {sp['external_ref']}" if sp["external_ref"] else ""
                print(f"  [{sp['id']:>3}] {sp['name']}{me}  —  {sp['recordings']} recordings, {vp}{ref}")
            return 0

        if action == "add":
            if not args.name:
                print("error: --name is required", file=sys.stderr)
                return 2
            sid = store.add_speaker(args.name, is_me=args.me,
                                    external_ref=args.ref or "", note=args.note or "")
            print(f"  speaker {sid}: {args.name}{' (you)' if args.me else ''}")
            print("  attribute a voice to them: plaudctl speakers name <recording> <label> "
                  f"--speaker {sid}")
            return 0

        if action == "unknown":
            rows = store.unnamed_labels()
            if not rows:
                print("  every diarized voice has a name")
                return 0
            print(f"  {len(rows)} unnamed voices, longest-speaking first:\n")
            for r in rows[: args.limit or 25]:
                when = time.strftime("%Y-%m-%d", time.localtime(r["started_at"]))
                name = r["title"] or r["filename"]
                mins = (r["seconds"] or 0) / 60
                print(f"  {r['recording_id']}  {r['label']:<12} {mins:5.1f} min  "
                      f"{when}  {name[:44]}")
            print("\n  name one:  plaudctl speakers name <recording_id> <label> --name Bayo")
            return 0

        if action == "name":
            if not (args.recording and args.label):
                print("error: recording id and label are required", file=sys.stderr)
                return 2
            if args.speaker:
                sid = args.speaker
            elif args.name:
                sid = store.add_speaker(args.name, is_me=args.me, external_ref=args.ref or "")
            elif args.clear:
                sid = None
            else:
                print("error: pass --name, --speaker or --clear", file=sys.stderr)
                return 2
            try:
                out = diarize.confirm(cfg, store, args.recording, args.label, sid)
            except KeyError:
                print(f"error: {args.recording} has no label {args.label}", file=sys.stderr)
                return 2
            who = store.speaker(sid)["name"] if sid else "(unnamed)"
            print(f"  {args.label} in {args.recording} is {who}")
            for spk_id, n in out["voiceprints"].items():
                sp = store.speaker(spk_id)
                if sp:
                    print(f"  voiceprint for {sp['name']}: {n} confirmed recording(s)")
            print("  transcript re-rendered with the new name")
            if sid:
                print("  apply it to the rest of the archive: plaudctl speakers rematch")
            return 0

        if action == "rematch":
            s = diarize.rematch(cfg, store, threshold=args.threshold)
            print(f"  considered {s['considered']} unnamed voices, matched {s['matched']} "
                  f"across {s['recordings']} recordings")
            print("  every match is a machine guess — check them: plaudctl speakers list")
            return 0

        if action == "link":
            if not (args.speaker and args.ref):
                print("error: --speaker and --ref are required", file=sys.stderr)
                return 2
            store.update_speaker(args.speaker, external_ref=args.ref)
            sp = store.speaker(args.speaker)
            print(f"  {sp['name']} -> {args.ref}")
            return 0

    print(f"error: unknown speakers action {action!r}", file=sys.stderr)
    return 2


def cmd_dispatch(args, cfg) -> int:
    """Hand accepted actions to an agent, and read back what it did."""
    with Store(cfg.db_path) as store:
        if args.action == "assign":
            try:
                d = dispatch.assign(cfg, store, args.id, args.agent,
                                    instructions=args.instructions or "")
            except KeyError:
                print(f"error: no action {args.id}", file=sys.stderr)
                return 2
            except (ValueError, dispatch.NotDispatchable) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"  dispatch {d['id']}: action {args.id} queued for {d['agent']}")
            print(f"  the agent picks it up with the MCP tool my_tasks(\"{d['agent']}\")")
            return 0

        if args.action == "list":
            rows = store.dispatches(agent=args.agent, status=args.status)
            if not rows:
                print("  nothing dispatched")
                return 0
            for r in rows:
                seen = "" if r["reviewed_at"] or r["status"] in ("queued", "claimed") else "  *unreviewed*"
                print(f"  [{r['id']:>3}] {r['status']:<10} {r['agent']:<10} {r['action_text'][:52]}{seen}")
                if r["result"]:
                    print(f"        -> {' '.join(r['result'].split())[:100]}")
                if r["error"]:
                    print(f"        !! {' '.join(r['error'].split())[:100]}")
            return 0

        if args.action == "cancel":
            try:
                dispatch.cancel(store, args.id)
            except KeyError:
                print(f"error: no dispatch {args.id}", file=sys.stderr)
                return 2
            except dispatch.NotDispatchable as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"  dispatch {args.id} cancelled")
            return 0

        if args.action == "agents":
            s = dispatch.summary(cfg, store)
            print(f"  configured: {', '.join(dispatch.agents(cfg)) or '(none)'}")
            for agent, counts in s["agents"].items():
                live = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
                print(f"    {agent:<12} {live}")
            print(f"  {s['open']} open, {s['unreviewed']} finished and unreviewed")
            return 0

    print(f"error: unknown dispatch action {args.action!r}", file=sys.stderr)
    return 2


def cmd_mcp(args, cfg) -> int:
    """Serve the archive to MCP clients over stdio."""
    from .mcp_server import serve

    tiers = None
    if args.tiers:
        tiers = {t.strip().lower() for t in args.tiers.split(",") if t.strip()}
    serve(tiers)
    return 0


def cmd_index(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = search.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  indexed {s['recordings']} recordings, {s['chunks']} chunks, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_search(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        hits = search.search(cfg, store, args.query, k=args.limit or 10,
                             include_excluded=args.excluded)
    if not hits:
        with Store(cfg.db_path) as store:
            n = store.index_stats(cfg.embed_model)["chunks"]
        print("  no matches." if n else "  nothing indexed yet — run: plaudctl index")
        return 0
    for h in hits:
        print(f"\n  {h['score']:.3f}  {h['started_iso']}  {h['filename'][:52]}  [{h['at']}]")
        body = " ".join(h["text"].split())
        print(f"        {body[:200]}{'…' if len(body) > 200 else ''}")
    print("\n  scores are cosine similarity, not confidence — compare them to each other")
    return 0


def cmd_story(args, cfg) -> int:
    """Draw a recording along its own duration, as SVG or an editable Excalidraw scene."""
    import json as _json

    import json as _json2
    if args.arc:
        with Store(cfg.db_path) as st:
            model = story.arc_story(cfg, st, days=args.days, themes=args.themes)
        if model.get("empty"):
            print(f"  {model['empty']}")
            return 1
        out = Path(args.out) if args.out else Path("arc.svg")
        out.write_text(story.arc_svg(model))
        print(f"  {model['recordings']} recordings · {model['chunks']} passages · {model['span']}")
        for t in model["themes"]:
            print(f"    {t['share']*100:4.1f}%  {t['recordings']:2} recs  {t['name']}")
        print(f"  wrote {out}")
        return 0

    with Store(cfg.db_path) as st:
        rid = args.recording
        if not rid:
            row = st.db.execute(
                "SELECT recording_id FROM sentiment ORDER BY scored_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("  nothing scored yet — run: plaudctl run")
                return 1
            rid = row[0]
        try:
            model = story.recording_story(cfg, st, rid)
        except KeyError:
            print(f"error: no recording {rid}", file=sys.stderr)
            return 1

    excal = args.format == "excalidraw"
    out = Path(args.out) if args.out else Path(
        f"story-{rid}.{'excalidraw' if excal else 'svg'}"
    )
    out.write_text(
        _json.dumps(story.to_excalidraw(model), indent=1) if excal else story.to_svg(model)
    )
    print(f"  {model['title'][:60]}")
    print(f"  {model['duration_min']} min · {len(model['segments'])} tone segments · "
          f"{len(model['pins'])} commitments pinned")
    print(f"  wrote {out}")
    return 0


def cmd_tier(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = tiering.sync(cfg, store)
    print(f"  {tiering.stack_dir(cfg)}")
    print(f"  approved {s['approved']} (+{s['added']} added, {s['updated']} updated, -{s['removed']} removed)")
    return 0


def cmd_init(args, cfg) -> int:
    return setup_wizard.run()


def cmd_service(args, cfg) -> int:
    if args.action == "install":
        hours = [int(h) for h in args.hours.split(",")] if args.hours else None
        return service.install(hours)
    if args.action == "uninstall":
        return service.uninstall()
    return service.status()


def cmd_web(args, cfg) -> int:
    from .web import serve

    serve(port=args.port or cfg.web_port)
    return 0


def cmd_run(args, cfg) -> int:
    """The daily pipeline. Never prunes — that stays a deliberate, separate act."""
    started = time.time()
    try:
        with runlock.acquire(cfg.archive_root, what="run"):
            rc = _run_stages(args, cfg)
    except runlock.RunInProgress as exc:
        print(f"  skipped: {exc}")
        return 0  # not an error — the scheduler colliding with a manual run is normal
    runlock.record_run(cfg.archive_root, started=started, rc=rc)
    return rc


def _run_stages(args, cfg) -> int:
    print("== sync ==")
    rc = cmd_sync(args, cfg)
    print("\n== transcribe ==")
    rc |= cmd_transcribe(args, cfg)
    print("\n== diarize ==")
    try:
        rc |= cmd_diarize(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== summarize ==")
    try:
        rc |= cmd_summarize(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    # After summarize: the summary is a far better title source than raw ASR.
    print("\n== title ==")
    try:
        rc |= cmd_title(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    # Before notes, so the frontmatter carries the reading rather than lagging a run.
    print("\n== sentiment ==")
    try:
        rc |= cmd_sentiment(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== notes ==")
    rc |= cmd_notes(args, cfg)
    print("\n== extract ==")
    try:
        rc |= cmd_extract(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== index ==")
    try:
        rc |= cmd_index(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== tier sync ==")
    rc |= cmd_tier(args, cfg)
    return rc


def cmd_status(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        c = store.counts()
        rows = store.all()
    from . import llm as _llm

    tr_ok, tr_why = transcribe.backend_status(cfg)
    llm_ok, llm_why = _llm.available(cfg)
    print(f"  archive:  {cfg.archive_root}")
    print(f"  vault:    {cfg.notes_dir or '(none configured)'}")
    print(f"  asr:      {'ok ' if tr_ok else 'XX '}{tr_why}")
    print(f"  llm:      {'ok ' if llm_ok else 'XX '}{cfg.llm_label} — {llm_why}"
          + ("" if _llm.is_local(cfg) else "  [remote: transcripts leave this machine]"))
    print(f"  account:  {cfg.email or '(not logged in)'} @ {cfg.api_base}")
    tok = auth.stored_token(cfg)
    if tok:
        exp = auth.token_expiry(tok)
        print(f"  token:    valid until {time.strftime('%Y-%m-%d', time.localtime(exp)) if exp else '?'}")
    else:
        print("  token:    none — run: plaudctl login <email>")
    print()
    print(f"  recordings known:   {c['total']}")
    print(f"    downloaded:       {c['downloaded']}")
    print(f"    download verified:{c['verified']}   (complete, not truncated)")
    print(f"    byte-identical:   {c['md5_exact']}   (matches Plaud's md5; rest carry an ID3 tag)")
    print(f"    transcribed:      {c['transcribed']}")
    print(f"    summarized:       {c['summarized']}")
    print(f"    tone scored:      {c['scored']}")
    print(f"    noted in vault:   {c['noted']}")
    print(f"    pruned from cloud:{c['pruned']}")
    hours = sum(r["duration_s"] or 0 for r in rows) / 3600
    gb = sum(r["audio_size"] or 0 for r in rows) / 1e9
    print(f"\n  {hours:.1f} hours of audio, {gb:.2f} GB on disk")

    print("\n  freshness:   (plaudctl fresh --cloud also checks Plaud for new recordings)")
    with Store(cfg.db_path) as store:
        _print_freshness(freshness.report(cfg, store))
    if not prune.receipt_path(cfg).exists():
        print("\n  prune: LOCKED (no probe receipt)")
    else:
        print("\n  prune: unlocked")
    return 0


def _print_freshness(report: dict) -> None:
    mark = "ok " if report["up_to_date"] else "-> "
    print(f"  {mark}{report['headline']}")
    for item in report["pending"]:
        print(f"      {item['count']:>4} not yet {item['label']:<28} {item['fix']}")
    if report["missing_notes"]:
        print(f"      {len(report['missing_notes']):>4} notes recorded but gone from the vault"
              "   plaudctl notes --force")
        for n in report["missing_notes"][:5]:
            print(f"           {n['path']}")
    s = report["stack"]
    if s["drifted"]:
        print(f"      stack corpus drifted: +{s['add']} ~{s['update']} -{s['remove']}"
              "        plaudctl tier")
    r = report["remote"]
    if r["checked"]:
        print(f"      cloud holds {r['cloud_total']}, {r['new']} not yet archived here")
        for n in r["newest"][:5]:
            print(f"           {n['started_iso']}  {n['filename'][:50]} ({n['duration_min']} min)")
    elif r.get("detail"):
        print(f"      cloud not checked — {r['detail']}")
    else:
        print("      cloud not checked")
    if (report["untriaged"] or report["proposed_actions"]
            or report.get("unnamed_voices") or report.get("unreviewed_dispatches")):
        print("\n  waiting on you (not on the pipeline):")
        if report["untriaged"]:
            oldest = report["oldest_untriaged_days"]
            print(f"      {report['untriaged']:>4} recordings untriaged"
                  + (f", oldest {oldest} days" if oldest else ""))
        if report["proposed_actions"]:
            print(f"      {report['proposed_actions']:>4} proposed actions awaiting review")
        if report.get("unnamed_voices"):
            print(f"      {report['unnamed_voices']:>4} voices with no name"
                  f" across {report['unnamed_recordings']} recordings"
                  "   plaudctl speakers unknown")
        if report.get("unreviewed_dispatches"):
            print(f"      {report['unreviewed_dispatches']:>4} agent reports you have not read"
                  "        plaudctl dispatch list")


def cmd_fresh(args, cfg) -> int:
    """Is the vault current? Local always; the cloud only when asked."""
    client = None
    with Store(cfg.db_path) as store:
        try:
            if args.cloud:
                client = _client(cfg)
            report = freshness.report(cfg, store, client=client)
        finally:
            if client is not None:
                client.close()

    last = runlock.last_run(cfg.archive_root)
    if last:
        ago = (time.time() - last["finished"]) / 3600
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["finished"]))
        print(f"  last full run: {when} ({ago:.1f}h ago, exit {last['rc']})")
    else:
        print("  last full run: never recorded — run: plaudctl run")
    _print_freshness(report)
    if not args.cloud:
        print("\n  add --cloud to also check Plaud for recordings not yet downloaded")
    return 0 if report["up_to_date"] else 1


def cmd_prune(args, cfg) -> int:
    with _client(cfg) as client, Store(cfg.db_path) as store:
        if args.probe:
            return 0 if prune.probe(cfg, client, store, confirm=args.yes) else 1
        s = prune.run(cfg, client, store, confirm=args.yes, limit=args.limit)
    print(f"\n  pruned {s['pruned']}, skipped {s['skipped']}, failed {s['failed']}")
    return 1 if s["failed"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="plaudctl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn, limit=None, force=False, yes=False, probe=False, cloud=False,
                        suggestions=False, excluded=False, query='',
                        recording=None, format='svg', out=None,
                        arc=False, themes=8, days=None,
                        speakers=None, name=None, ref=None, note=None, me=False,
                        label=None, speaker=None, clear=False, threshold=None,
                        agent=None, status=None, instructions=None, id=None,
                        tiers=None)
        return sp

    sp = add("login", cmd_login, "authenticate with Plaud (emailed one-time code)")
    sp.add_argument("email")

    add("logout", cmd_logout, "remove the stored token from the Keychain")
    add("status", cmd_status, "show archive + pipeline state")

    sp = add("fresh", cmd_fresh, "is the vault up to date? what still needs processing?")
    sp.add_argument("--cloud", action="store_true",
                    help="also ask Plaud for recordings not yet downloaded")

    sp = add("sync", cmd_sync, "download new recordings from the Plaud cloud")
    sp.add_argument("--limit", type=int)

    add("verify", cmd_verify, "re-hash the local archive and flag drift")

    for name, fn, help_ in (
        ("transcribe", cmd_transcribe, "transcribe locally with mlx-whisper"),
        ("summarize", cmd_summarize, "summarize locally with Ollama"),
        ("sentiment", cmd_sentiment, "score the tone of transcripts (feeds the trend chart)"),
        ("notes", cmd_notes, "write/refresh Obsidian notes"),
        ("extract", cmd_extract, "propose commitments from transcripts (review in the console)"),
    ):
        sp = add(name, fn, help_)
        sp.add_argument("--limit", type=int)
        sp.add_argument("--force", action="store_true", help="redo already-processed items")

    sub.choices["extract"].add_argument(
        "--suggestions", action="store_true",
        help="also propose implied next steps, not just stated commitments (noisy)")

    sp = add("index", cmd_index, "embed transcripts for semantic search")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--force", action="store_true", help="re-embed everything")

    sp = add("search", cmd_search, "semantic search across your transcripts")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, help="how many hits (default 10)")
    sp.add_argument("--excluded", action="store_true", help="also search excluded recordings")

    sp = add("story", cmd_story, "draw a recording along its own duration")
    sp.add_argument("recording", nargs="?", help="recording id (default: most recently scored)")
    sp.add_argument("--format", choices=["svg", "excalidraw"], default="svg")
    sp.add_argument("--out", help="output path")
    sp.add_argument("--arc", action="store_true",
                    help="draw the whole corpus over time instead of one recording")
    sp.add_argument("--themes", type=int, default=8, help="how many themes to cluster into")
    sp.add_argument("--days", type=int, help="limit the arc to the last N days")

    sp = add("title", cmd_title, "name recordings from their content, not their timestamp")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--force", action="store_true",
                    help="re-title machine-titled recordings (never your own)")

    sp = add("diarize", cmd_diarize, "split recordings by speaker and recognise known voices")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--force", action="store_true", help="re-diarize already-processed audio")
    sp.add_argument("--speakers", type=int,
                    help="force an exact speaker count (use when you know it)")

    sp = add("speakers", cmd_speakers, "name the voices in your recordings")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "status", "login", "add", "unknown", "name",
                             "rematch", "link"])
    sp.add_argument("recording", nargs="?", help="recording id (for `name`)")
    sp.add_argument("label", nargs="?", help="diarization label, e.g. SPEAKER_00")
    sp.add_argument("--name", help="the person's name")
    sp.add_argument("--speaker", type=int, help="an existing speaker id")
    sp.add_argument("--me", action="store_true", help="this speaker is you")
    sp.add_argument("--ref", help="external contact/CRM reference, e.g. clarify:rec_123")
    sp.add_argument("--note", help="free-text note about this person")
    sp.add_argument("--clear", action="store_true", help="un-name a label")
    sp.add_argument("--threshold", type=float, help="override the match threshold for `rematch`")
    sp.add_argument("--limit", type=int)

    sp = add("dispatch", cmd_dispatch, "assign accepted actions to an agent")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "assign", "cancel", "agents"])
    sp.add_argument("id", nargs="?", type=int, help="action id to assign, or dispatch id to cancel")
    sp.add_argument("--agent", help="which agent, e.g. openclaw")
    sp.add_argument("--instructions", help="anything the agent needs beyond the action text")
    sp.add_argument("--status", choices=list(dispatch.STATUSES))

    sp = add("mcp", cmd_mcp, "serve the archive to MCP clients over stdio")
    sp.add_argument("--tiers", help="override mcp_tier_scope, e.g. stack or stack,local")

    add("tier", cmd_tier, "reconcile PLAUD/stack/ with your triage decisions")

    sp = add("web", cmd_web, "open the console (triage, actions, measures)")
    sp.add_argument("--port", type=int, default=None)

    add("init", cmd_init, "interactive first-time setup")

    sp = add("service", cmd_service, "install/remove the background services")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["install", "uninstall", "status"])
    sp.add_argument("--hours", help="comma-separated sync hours, e.g. 7,12,18,22")

    sp = add("run", cmd_run,
             "sync -> transcribe -> diarize -> summarize -> title -> sentiment -> notes -> extract -> index -> tier")
    sp.add_argument("--limit", type=int)

    sp = add("prune", cmd_prune, "move locally-archived recordings to Plaud's trash")
    sp.add_argument("--probe", action="store_true", help="verify the endpoint on ONE recording")
    sp.add_argument("--yes", action="store_true", help="actually send it (default is dry run)")
    sp.add_argument("--limit", type=int)

    args = p.parse_args(argv)
    cfg = load()
    # Every verb except login/logout touches the archive volume; fail loudly and
    # early rather than writing a phantom archive onto the boot disk.
    if args.cmd not in ("login", "logout"):
        try:
            cfg.check_archive_available()
        except ArchiveUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    try:
        return args.fn(args, cfg)
    except (auth.PlaudAuthError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
