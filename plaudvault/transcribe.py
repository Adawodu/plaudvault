"""Transcription, with a backend per platform.

  mlx             Apple Silicon GPU via mlx-whisper. Fastest here (~29x realtime on
                  an M4 Pro) but Apple-Silicon-only.
  faster-whisper  CTranslate2. CPU anywhere, CUDA if present. The portable default.
  openai          Any OpenAI-compatible /audio/transcriptions endpoint. Sends audio
                  off the machine — only pick this deliberately.

Audio is decoded in-process with PyAV rather than by shelling out to an `ffmpeg`
binary, so there is no system dependency to install (and no broken Homebrew ffmpeg
to debug).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .store import Store

SAMPLE_RATE = 16000


def transcript_paths(cfg: Config, rec_id: str) -> tuple[Path, Path]:
    return cfg.transcript_dir / f"{rec_id}.json", cfg.transcript_dir / f"{rec_id}.txt"


def _format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_audio(path: Path):
    """Decode any audio file to 16 kHz mono float32."""
    import av
    import numpy as np

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(
            format="fltp", layout="mono", rate=SAMPLE_RATE
        )
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))

    if not chunks:
        raise RuntimeError(f"no decodable audio in {path}")
    return np.concatenate(chunks).astype(np.float32)


# ---------------------------------------------------------------------- backends

_FW_MODEL = None  # faster-whisper models are expensive to construct; reuse one


def _transcribe_mlx(cfg: Config, audio: Path) -> dict:
    import mlx_whisper

    return mlx_whisper.transcribe(
        load_audio(audio),
        path_or_hf_repo=cfg.resolved_whisper_model,
        language=cfg.whisper_language or None,
        word_timestamps=False,
        condition_on_previous_text=False,  # long recordings otherwise drift into loops
        verbose=None,
    )


def _transcribe_faster_whisper(cfg: Config, audio: Path) -> dict:
    global _FW_MODEL
    from faster_whisper import WhisperModel

    if _FW_MODEL is None:
        device = cfg.whisper_device or "auto"
        compute = cfg.whisper_compute_type or ("float16" if device == "cuda" else "int8")
        _FW_MODEL = WhisperModel(cfg.resolved_whisper_model, device=device, compute_type=compute)

    segments, info = _FW_MODEL.transcribe(
        str(audio),
        language=cfg.whisper_language or None,
        condition_on_previous_text=False,
        vad_filter=True,
    )
    segs = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
    return {
        "text": "".join(s["text"] for s in segs),
        "segments": segs,
        "language": info.language,
    }


def _transcribe_openai(cfg: Config, audio: Path) -> dict:
    import httpx

    from .llm import _auth_headers

    with audio.open("rb") as fh:
        resp = httpx.post(
            f"{cfg.openai_base_url.rstrip('/')}/audio/transcriptions",
            headers=_auth_headers(cfg),
            files={"file": (audio.name, fh, "audio/mpeg")},
            data={
                "model": cfg.resolved_whisper_model,
                "response_format": "verbose_json",
                **({"language": cfg.whisper_language} if cfg.whisper_language else {}),
            },
            timeout=1800,
        )
    if not resp.is_success:
        raise RuntimeError(f"transcription API HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    return {
        "text": body.get("text", ""),
        "segments": [
            {"start": s.get("start", 0), "end": s.get("end", 0), "text": s.get("text", "")}
            for s in body.get("segments", [])
        ],
        "language": body.get("language"),
    }


_BACKENDS = {
    "mlx": _transcribe_mlx,
    "faster-whisper": _transcribe_faster_whisper,
    "openai": _transcribe_openai,
}


def transcribe_file(cfg: Config, audio: Path) -> dict:
    backend = cfg.resolved_transcribe_backend
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise RuntimeError(f"unknown transcribe_backend {backend!r}")
    try:
        return fn(cfg, audio)
    except ImportError as exc:
        pkg = {"mlx": "mlx-whisper", "faster-whisper": "faster-whisper"}.get(backend, backend)
        raise RuntimeError(
            f"backend {backend!r} needs the {pkg!r} package: pip install {pkg}"
        ) from exc


def backend_status(cfg: Config) -> tuple[bool, str]:
    """(usable, reason) — checked without doing any work."""
    backend = cfg.resolved_transcribe_backend
    if backend == "mlx":
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False, "mlx-whisper not installed (Apple Silicon only)"
        return True, f"mlx · {cfg.resolved_whisper_model}"
    if backend == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper not installed: pip install faster-whisper"
        return True, f"faster-whisper · {cfg.resolved_whisper_model}"
    if not cfg.openai_api_key() and "127.0.0.1" not in cfg.openai_base_url:
        return False, f"${cfg.openai_api_key_env} is not set"
    return True, f"api · {cfg.resolved_whisper_model}"


# ---------------------------------------------------------------------- driver


def run(cfg: Config, store: Store, *, limit: int | None = None, force: bool = False) -> dict:
    cfg.ensure_dirs()
    ok, reason = backend_status(cfg)
    if not ok:
        raise RuntimeError(reason)

    rows = store.all() if force else store.needing("transcript_path", requires="audio_sha256")
    rows = [r for r in rows if r["audio_path"] and Path(r["audio_path"]).exists()]
    if limit:
        rows = rows[:limit]

    model = cfg.resolved_whisper_model
    stats = {"done": 0, "failed": 0, "audio_seconds": 0.0, "wall_seconds": 0.0}
    print(f"  {len(rows)} to transcribe · {reason}")

    for i, row in enumerate(rows, 1):
        audio = Path(row["audio_path"])
        json_path, txt_path = transcript_paths(cfg, row["id"])
        mins = row["duration_s"] / 60
        print(f"  [{i}/{len(rows)}] {row['filename'][:60]} ({mins:.0f}m) ...", flush=True)
        t0 = time.time()
        try:
            result = transcribe_file(cfg, audio)
            elapsed = time.time() - t0

            json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            lines = [
                f"# {row['filename']}",
                f"# recorded: {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['started_at']))}",
                f"# duration: {mins:.1f} min | model: {model}",
                "",
            ]
            for seg in result.get("segments", []):
                lines.append(f"[{_format_ts(seg['start'])}] {seg['text'].strip()}")
            txt_path.write_text("\n".join(lines) + "\n")

            store.update(
                row["id"],
                transcript_path=str(txt_path),
                transcribed_at=int(time.time()),
                transcribe_model=model,
            )
            stats["done"] += 1
            stats["audio_seconds"] += row["duration_s"]
            stats["wall_seconds"] += elapsed
            print(f"    done in {elapsed:.0f}s ({row['duration_s'] / elapsed:.0f}x realtime)")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    [fail] {exc}")

    return stats


def read_transcript(cfg: Config, rec_id: str) -> str:
    _, txt = transcript_paths(cfg, rec_id)
    if not txt.exists():
        return ""
    return "\n".join(ln for ln in txt.read_text().splitlines() if not ln.startswith("# "))
