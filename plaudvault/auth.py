"""Plaud OTP login. The long-lived user token lives in the OS keyring, never in the config file.

Plaud has no password login for the API — you get an emailed one-time code, trade it
for a JWT "user token" (UT), and that UT then mints short-lived workspace tokens (WT)
for the data endpoints. Only the UT is worth storing: a WT dies in ~24h and cannot
mint its own replacement.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import httpx

from .config import Config, save_field

# Plaud's API rejects unrecognised clients; present as a normal browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_REGION_REDIRECTS = 3


class PlaudAuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- secrets
#
# `keyring` covers macOS Keychain, Windows Credential Locker, and Linux SecretService.
# Headless Linux boxes often have no SecretService at all, so rather than failing we
# fall back to a 0600 file next to the config — and say so, because that is a real
# downgrade in protection and the user deserves to know.


def _fallback_path(service: str, account: str) -> Path:
    from .config import CONFIG_PATH

    digest = hashlib.sha256(f"{service}:{account}".encode()).hexdigest()[:16]
    return CONFIG_PATH.parent / f"token-{digest}"


def keychain_set(service: str, account: str, secret: str) -> None:
    try:
        import keyring

        keyring.set_password(service, account, secret)
        return
    except Exception:  # noqa: BLE001
        pass
    path = _fallback_path(service, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    path.chmod(0o600)
    print(f"  [warn] no system keyring available; token stored at {path} (mode 0600)")


def keychain_get(service: str, account: str) -> str | None:
    try:
        import keyring

        secret = keyring.get_password(service, account)
        if secret:
            return secret
    except Exception:  # noqa: BLE001
        pass
    path = _fallback_path(service, account)
    return path.read_text().strip() if path.exists() else None


def keychain_delete(service: str, account: str) -> bool:
    removed = False
    try:
        import keyring

        keyring.delete_password(service, account)
        removed = True
    except Exception:  # noqa: BLE001
        pass
    path = _fallback_path(service, account)
    if path.exists():
        path.unlink()
        removed = True
    return removed


# --------------------------------------------------------------------------- jwt


def decode_claims(token: str) -> dict:
    """Decode JWT claims WITHOUT verifying. Diagnostic only — never authorise from this."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def token_expiry(token: str) -> float | None:
    exp = decode_claims(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def is_workspace_token(token: str) -> bool:
    claims = decode_claims(token)
    return "ut_ref" in claims or "wid" in claims


# --------------------------------------------------------------------------- otp flow


def send_code(email: str, api_base: str, _depth: int = 0) -> tuple[str, str]:
    """Request an OTP email. Returns (otp_token, resolved_api_base).

    Plaud shards accounts by region and answers status -302 with the correct host,
    so an EU/APAC account silently works without the user knowing their region.
    """
    resp = httpx.post(
        f"{api_base}/auth/otp-send-code",
        json={"username": email},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    body = resp.json()

    if body.get("status") == -302:
        regional = (body.get("data") or {}).get("domains", {}).get("api")
        if regional and _depth < MAX_REGION_REDIRECTS:
            return send_code(email, regional.rstrip("/"), _depth + 1)
        raise PlaudAuthError("Plaud region redirect loop")

    if body.get("status") != 0 or not body.get("token"):
        raise PlaudAuthError(body.get("msg") or "Failed to send verification code")

    return body["token"], api_base


def verify_code(code: str, otp_token: str, api_base: str) -> str:
    resp = httpx.post(
        f"{api_base}/auth/otp-login",
        json={"code": code, "token": otp_token},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    body = resp.json()
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    if not token:
        raise PlaudAuthError(body.get("msg") or "Invalid verification code")
    if is_workspace_token(token):
        raise PlaudAuthError(
            "Plaud returned a workspace token, not a user token. "
            "Workspace tokens expire in ~24h and cannot be refreshed."
        )
    return token


def login(cfg: Config, email: str, code_reader=input) -> str:
    """Interactive OTP login. Stores the user token in the OS keyring and returns it."""
    otp_token, api_base = send_code(email, cfg.api_base)
    if api_base != cfg.api_base:
        save_field("api_base", api_base)
        print(f"  regional server detected: {api_base}")
    print(f"  verification code sent to {email}")
    code = code_reader("  enter the 6-digit code: ").strip()
    token = verify_code(code, otp_token, api_base)
    keychain_set(cfg.keychain_service, email, token)
    save_field("email", email)
    exp = token_expiry(token)
    when = time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "unknown"
    print(f"  stored in the OS keyring (service={cfg.keychain_service}); token expires {when}")
    return token


def stored_token(cfg: Config) -> str | None:
    if not cfg.email:
        return None
    return keychain_get(cfg.keychain_service, cfg.email)


def require_token(cfg: Config) -> str:
    token = stored_token(cfg)
    if not token:
        raise PlaudAuthError("Not logged in. Run: plaudctl login <email>")
    exp = token_expiry(token)
    if exp and exp < time.time():
        raise PlaudAuthError("Plaud token expired. Run: plaudctl login <email>")
    return token
