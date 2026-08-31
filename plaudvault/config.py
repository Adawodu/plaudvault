"""Configuration.

Defaults are deliberately generic — nothing here should reference the machine it was
developed on. Precedence: environment (`PLAUDVAULT_<KEY>`) > config file > defaults.
Run `plaudctl init` for an interactive first-time setup.
"""

from __future__ import annotations

import os
import platform
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _default_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "plaudvault"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "plaudvault"


CONFIG_PATH = Path(os.environ.get("PLAUDVAULT_CONFIG", _default_config_dir() / "config.toml"))


def _default_archive_root() -> str:
    """A directory the user certainly has. Point this at a big disk during `init`."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return str(base / "plaudvault")


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


DEFAULTS: dict = {
    # ---- storage ----------------------------------------------------------
    "archive_root": _default_archive_root(),
    # Optional. Empty means "don't write notes anywhere" — the console still works.
    "vault_root": "",
    "vault_subdir": "Plaud",

    # ---- plaud account ----------------------------------------------------
    "api_base": "https://api.plaud.ai",
    "prefer_opus": False,

    # ---- transcription ----------------------------------------------------
    # auto | mlx | faster-whisper | openai
    #   auto           pick mlx on Apple Silicon, else faster-whisper
    #   mlx            mlx-whisper, Apple Silicon GPU
    #   faster-whisper CTranslate2, runs on CPU/CUDA anywhere
    #   openai         any OpenAI-compatible /v1/audio/transcriptions endpoint
    "transcribe_backend": "auto",
    # Left empty, each backend picks its own sensible default model.
    "whisper_model": "",
    "whisper_language": "en",
    # faster-whisper only: cpu | cuda | auto
    "whisper_device": "auto",
    "whisper_compute_type": "",

    # ---- language model (summaries + action extraction) -------------------
    # ollama | openai   ("openai" = any OpenAI-compatible chat completions API)
    "llm_provider": "ollama",
    "ollama_host": "http://127.0.0.1:11434",
    "ollama_model": "qwen3.5:latest",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",
    # Name of the env var holding the key. The key itself is never stored here.
    "openai_api_key_env": "OPENAI_API_KEY",

    # ---- semantic search --------------------------------------------------
    # Embeddings always go through Ollama, even when the chat model is remote:
    # indexing sends every sentence you have ever recorded, which is a far larger
    # disclosure than summarizing one file, and should not follow that setting.
    "embed_model": "nomic-embed-text",

    # ---- speakers ---------------------------------------------------------
    # Diarization splits audio into anonymous SPEAKER_00/01/... turns and returns one
    # embedding per speaker. Naming a label once stores that embedding as a voiceprint,
    # so the same voice is recognised in every later recording.
    #
    # The models are gated on HuggingFace: free, but you must accept the licence on
    # each model page and hold a token. `plaudctl speakers status` says exactly which.
    "diarize_model": "pyannote/speaker-diarization-community-1",
    # Name of the env var holding the HF token. The token itself is stored in the OS
    # keyring (`plaudctl speakers login`), never in this file.
    "hf_token_env": "HF_TOKEN",
    # Cosine similarity above which an unnamed voice is auto-attributed to a known
    # person. pyannote's embeddings put the same speaker around 0.7-0.9 and different
    # speakers around 0.2-0.45, so 0.65 leaves a real gap on both sides. An automatic
    # match is always shown as a machine guess and never feeds another voiceprint.
    "speaker_match_threshold": 0.65,
    # Diarizing a 20-second memo produces one speaker and costs a model load.
    "diarize_min_seconds": 60,

    # ---- agent dispatch / MCP ---------------------------------------------
    # Which tiers an MCP client may read. 'stack' is the strictest (see the product
    # bible §9); the default matches what the local console shows, because the MCP
    # server binds to stdio on this machine for your own agents.
    # Comma-separated: any of stack, local, untriaged.
    "mcp_tier_scope": "stack,local,untriaged",
    # Agents an action may be assigned to. Free text, but an allow-list means a typo
    # cannot park a job in a queue nobody polls.
    "agents": "openclaw,claude",

    # ---- behaviour --------------------------------------------------------
    # Extract only what someone actually committed to. Turning this on also asks for
    # implied next steps, which a small local model produces in bulk: on a real ~20-hour
    # corpus it returned 198 suggestions against 57 commitments, and the suggestions
    # were mostly topic summaries. A board nobody opens measures nothing.
    "extract_suggestions": False,
    "summarize_min_seconds": 120,
    "prune_min_age_days": 14,
    "web_port": 8787,
    "keychain_service": "plaudvault",
}


class ArchiveUnavailable(RuntimeError):
    """The archive volume is not mounted."""


@dataclass
class Config:
    archive_root: Path
    vault_root: Path | None
    vault_subdir: str
    api_base: str
    prefer_opus: bool
    transcribe_backend: str
    whisper_model: str
    whisper_language: str
    whisper_device: str
    whisper_compute_type: str
    llm_provider: str
    ollama_host: str
    ollama_model: str
    openai_base_url: str
    openai_model: str
    openai_api_key_env: str
    embed_model: str
    diarize_model: str
    hf_token_env: str
    speaker_match_threshold: float
    diarize_min_seconds: int
    mcp_tier_scope: str
    agents: str
    extract_suggestions: bool
    summarize_min_seconds: int
    prune_min_age_days: int
    web_port: int
    keychain_service: str
    email: str | None = field(default=None)

    # ---- derived paths ----------------------------------------------------

    @property
    def audio_dir(self) -> Path:
        return self.archive_root / "audio"

    @property
    def meta_dir(self) -> Path:
        return self.archive_root / "meta"

    @property
    def transcript_dir(self) -> Path:
        return self.archive_root / "transcripts"

    @property
    def summary_dir(self) -> Path:
        return self.archive_root / "summaries"

    @property
    def diarization_dir(self) -> Path:
        return self.archive_root / "diarization"

    @property
    def db_path(self) -> Path:
        return self.archive_root / "manifest.sqlite"

    @property
    def notes_dir(self) -> Path | None:
        """None when no vault is configured — note writing is then skipped."""
        return (self.vault_root / self.vault_subdir) if self.vault_root else None

    # ---- resolution -------------------------------------------------------

    @property
    def resolved_transcribe_backend(self) -> str:
        if self.transcribe_backend != "auto":
            return self.transcribe_backend
        return "mlx" if is_apple_silicon() else "faster-whisper"

    @property
    def resolved_whisper_model(self) -> str:
        if self.whisper_model:
            return self.whisper_model
        return {
            "mlx": "mlx-community/whisper-large-v3-turbo",
            "faster-whisper": "large-v3-turbo",
            "openai": "whisper-1",
        }[self.resolved_transcribe_backend]

    @property
    def llm_label(self) -> str:
        return (
            f"ollama:{self.ollama_model}"
            if self.llm_provider == "ollama"
            else f"{self.openai_base_url}:{self.openai_model}"
        )

    def openai_api_key(self) -> str:
        return os.environ.get(self.openai_api_key_env, "")

    def hf_token(self) -> str:
        """The HuggingFace token, environment first, then the OS keyring.

        Environment wins so a CI or one-off run can override without touching the
        keyring, which is where `plaudctl speakers login` puts it.
        """
        env = os.environ.get(self.hf_token_env, "")
        if env:
            return env
        try:
            from .auth import keychain_get

            return keychain_get(self.keychain_service, "huggingface") or ""
        except Exception:  # noqa: BLE001 — no keyring backend is a valid state
            return ""

    @property
    def agent_names(self) -> list[str]:
        return [a.strip().lower() for a in self.agents.split(",") if a.strip()]

    @property
    def mcp_tiers(self) -> set[str]:
        return {t.strip().lower() for t in self.mcp_tier_scope.split(",") if t.strip()}

    # ---- safety -----------------------------------------------------------

    def check_archive_available(self) -> None:
        """Refuse to write if the archive lives on a volume that isn't mounted.

        Without this, an unplugged external drive means the OS happily creates the
        archive path on the boot disk, the pipeline "succeeds", and the phantom copy
        is shadowed the moment the real drive returns — a silent split-brain archive
        that `prune` would later trust.
        """
        root = self.archive_root.expanduser()
        parts = root.resolve().parts
        mount_parent = None
        if sys.platform == "darwin" and len(parts) > 2 and parts[1] == "Volumes":
            mount_parent = Path("/") / parts[1] / parts[2]
        elif sys.platform.startswith("linux") and len(parts) > 3 and parts[1] in ("mnt", "media"):
            mount_parent = Path("/") / parts[1] / parts[2] / parts[3]
        if mount_parent and not os.path.ismount(mount_parent):
            raise ArchiveUnavailable(
                f"Archive volume {mount_parent} is not mounted. "
                "Connect the drive, or point archive_root elsewhere."
            )

    def ensure_dirs(self) -> None:
        self.check_archive_available()
        dirs = [self.audio_dir, self.meta_dir, self.transcript_dir, self.summary_dir,
                self.diarization_dir]
        if self.notes_dir:
            dirs.append(self.notes_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


def load() -> Config:
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            data.update(tomllib.load(fh))
    for key in list(data):
        env = os.environ.get(f"PLAUDVAULT_{key.upper()}")
        if env is None:
            continue
        default = DEFAULTS.get(key)
        if isinstance(default, bool):
            data[key] = env.lower() in ("1", "true", "yes")
        elif isinstance(default, int) and not isinstance(default, bool):
            data[key] = int(env)
        elif isinstance(default, float):
            data[key] = float(env)
        else:
            data[key] = env

    data["archive_root"] = Path(str(data["archive_root"])).expanduser()
    data["vault_root"] = Path(str(data["vault_root"])).expanduser() if data["vault_root"] else None
    known = {f for f in Config.__dataclass_fields__}
    return Config(**{k: v for k, v in data.items() if k in known})


def _fmt(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(values: dict) -> None:
    """Merge `values` into the config file, preserving anything already there."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            existing = tomllib.load(fh)
    existing.update(values)
    CONFIG_PATH.write_text(
        "# plaudvault configuration — see `plaudctl init` or the README\n"
        + "\n".join(f"{k} = {_fmt(v)}" for k, v in sorted(existing.items()))
        + "\n"
    )
    CONFIG_PATH.chmod(0o600)


def save_field(key: str, value) -> None:
    save({key: value})
