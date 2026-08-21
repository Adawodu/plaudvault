"""`plaudctl init` — first-run configuration.

Asks only what can't be guessed, shows the detected defaults, and writes a config
file. Safe to re-run; existing values become the defaults you can accept with Enter.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import llm, transcribe
from .config import DEFAULTS, CONFIG_PATH, is_apple_silicon, load, save


def _ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{shown}: ").strip()
    except EOFError:
        return default
    return answer or default


def _ask_choice(prompt: str, options: list[str], default: str) -> str:
    while True:
        answer = _ask(f"{prompt} ({'/'.join(options)})", default)
        if answer in options:
            return answer
        print(f"    pick one of: {', '.join(options)}")


def _free_gb(path: Path) -> float | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return None


def run() -> int:
    cfg = load()
    print("\nplaudvault setup\n" + "─" * 60)
    print(f"  config file: {CONFIG_PATH}\n")

    # ---- storage ---------------------------------------------------------
    print("Storage. Audio is roughly 15 MB per hour recorded.")
    archive = _ask("Archive directory", str(cfg.archive_root))
    archive_path = Path(archive).expanduser()
    free = _free_gb(archive_path)
    if free is not None:
        print(f"    {free:.0f} GB free — about {free * 1000 / 15:.0f} hours of audio")

    print("\nNotes. Optional: writes one markdown note per recording (Obsidian or any")
    print("folder). Leave blank to skip — the console works without it.")
    vault = _ask("Notes folder (blank for none)", str(cfg.vault_root or ""))

    # ---- transcription ---------------------------------------------------
    print("\nTranscription runs on your machine by default.")
    if is_apple_silicon():
        print("    Apple Silicon detected — mlx-whisper will use the GPU.")
    else:
        print("    Non-Apple-Silicon — faster-whisper will be used (CPU, or CUDA if present).")
    backend = _ask_choice(
        "Backend", ["auto", "mlx", "faster-whisper", "openai"], cfg.transcribe_backend
    )
    language = _ask("Primary language code (blank = autodetect)", cfg.whisper_language)

    # ---- llm -------------------------------------------------------------
    print("\nSummaries and action extraction need a language model.")
    print("    ollama = fully local, nothing leaves the machine (recommended)")
    print("    openai = any OpenAI-compatible endpoint, including LM Studio or a hosted API")
    provider = _ask_choice("Provider", ["ollama", "openai"], cfg.llm_provider)

    values = {
        "archive_root": str(archive_path),
        "vault_root": str(Path(vault).expanduser()) if vault else "",
        "transcribe_backend": backend,
        "whisper_language": language,
        "llm_provider": provider,
    }

    if provider == "ollama":
        values["ollama_host"] = _ask("Ollama host", cfg.ollama_host)
        values["ollama_model"] = _ask("Ollama model", cfg.ollama_model)
    else:
        values["openai_base_url"] = _ask("Base URL", cfg.openai_base_url)
        values["openai_model"] = _ask("Model", cfg.openai_model)
        values["openai_api_key_env"] = _ask(
            "Env var holding the API key", cfg.openai_api_key_env
        )
        print(f"    The key itself is never written to config — export ${values['openai_api_key_env']}.")

    values["web_port"] = int(_ask("Console port", str(cfg.web_port)))

    save(values)
    print(f"\n  saved {CONFIG_PATH}\n")

    # ---- verify ----------------------------------------------------------
    cfg = load()
    print("Checks")
    try:
        cfg.ensure_dirs()
        print(f"  ✓ archive writable: {cfg.archive_root}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ archive: {exc}")

    ok, why = transcribe.backend_status(cfg)
    print(f"  {'✓' if ok else '✗'} transcription: {why}")

    ok, why = llm.available(cfg)
    print(f"  {'✓' if ok else '✗'} language model: {cfg.llm_label} — {why}")
    if ok and not llm.is_local(cfg):
        print("      note: this provider is remote — transcript text will leave this machine.")

    print("\nNext")
    print("  plaudctl login <your-plaud-email>    authenticate with Plaud")
    print("  plaudctl run                          sync, transcribe, summarize, extract")
    print("  plaudctl web                          open the console")
    print("  plaudctl service install              run it automatically\n")
    return 0
