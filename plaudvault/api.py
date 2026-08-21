"""Thin Plaud API client.

Endpoint surface derived from the Riffado/openplaud client (AGPL-3.0) — the only
public documentation of Plaud's private API that exists. Auth model: a long-lived
user token (UT) mints a short-lived workspace token (WT); data endpoints want the WT
but accept the UT on personal accounts, so we fall back rather than fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import USER_AGENT

MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0


class PlaudApiError(RuntimeError):
    pass


class PlaudTokenError(PlaudApiError):
    """Token rejected — re-login required."""


@dataclass
class Recording:
    id: str
    filename: str
    filesize: int
    file_md5: str
    duration_ms: int
    start_time_ms: int
    is_trash: bool
    is_trans: bool
    is_summary: bool
    serial_number: str
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "Recording":
        return cls(
            id=d["id"],
            filename=d.get("filename") or d.get("fullname") or d["id"],
            filesize=int(d.get("filesize") or 0),
            file_md5=(d.get("file_md5") or "").lower(),
            duration_ms=int(d.get("duration") or 0),
            start_time_ms=int(d.get("start_time") or 0),
            is_trash=bool(d.get("is_trash")),
            is_trans=bool(d.get("is_trans")),
            is_summary=bool(d.get("is_summary")),
            serial_number=d.get("serial_number") or "",
            raw=d,
        )

    @property
    def started_at(self) -> time.struct_time:
        return time.localtime(self.start_time_ms / 1000)

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000


class PlaudClient:
    def __init__(self, user_token: str, api_base: str, workspace_id: str | None = None):
        self._ut = user_token
        self.api_base = api_base.rstrip("/")
        self._wt: str | None = None
        self._workspace_id = workspace_id
        self._ut_fallback = False
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PlaudClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- workspace auth

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _ensure_workspace_token(self) -> None:
        if self._wt or self._ut_fallback:
            return
        try:
            if not self._workspace_id:
                self._workspace_id = self._pick_workspace_id()
            self._wt = self._mint_workspace_token(self._workspace_id)
        except PlaudTokenError:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade, don't die
            print(f"  [warn] workspace token mint failed ({exc}); using user token")
            self._ut_fallback = True

    def _pick_workspace_id(self) -> str:
        resp = self._http.get(
            f"{self.api_base}/team-app/workspaces/list",
            params={"need_personal_workspace": "true"},
            headers=self._headers(self._ut),
        )
        if resp.status_code == 401:
            raise PlaudTokenError("Plaud rejected the user token")
        resp.raise_for_status()
        body = resp.json()
        workspaces = (body.get("data") or {}).get("workspaces") or []
        if not workspaces:
            raise PlaudApiError("Plaud account has no workspaces")
        personal = next((w for w in workspaces if str(w.get("workspace_type")) == "0"), None)
        return (personal or workspaces[0])["workspace_id"]

    def _mint_workspace_token(self, workspace_id: str) -> str:
        resp = self._http.post(
            f"{self.api_base}/user-app/auth/workspace/token/{workspace_id}",
            headers=self._headers(self._ut),
            content="{}",
        )
        if resp.status_code == 401:
            raise PlaudTokenError("Plaud rejected the user token")
        resp.raise_for_status()
        body = resp.json()
        token = (body.get("data") or {}).get("workspace_token")
        if not token:
            raise PlaudApiError(body.get("msg") or "Failed to mint workspace token")
        return token

    # ----------------------------------------------------------------- request core

    def _request(self, method: str, path: str, **kw) -> Any:
        self._ensure_workspace_token()
        bearer = self._wt or self._ut
        url = f"{self.api_base}{path}"

        for attempt in range(MAX_RETRIES + 1):
            resp = self._http.request(method, url, headers=self._headers(bearer), **kw)

            if resp.status_code == 429 and attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else INITIAL_BACKOFF * 2**attempt
                time.sleep(delay)
                continue

            if resp.status_code == 401:
                # A dead WT is recoverable; a dead UT is not.
                if self._wt and attempt < MAX_RETRIES:
                    self._wt = None
                    self._ensure_workspace_token()
                    bearer = self._wt or self._ut
                    continue
                raise PlaudTokenError(
                    "Plaud rejected the access token. Run: plaudctl login <email>"
                )

            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF * 2**attempt)
                continue

            if not resp.is_success:
                raise PlaudApiError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")

            return resp.json()

        raise PlaudApiError(f"{method} {path} failed after {MAX_RETRIES} retries")

    # ----------------------------------------------------------------- endpoints

    def devices(self) -> list[dict]:
        body = self._request("GET", "/device/list")
        if body.get("status") != 0 or not isinstance(body.get("data_devices"), list):
            raise PlaudApiError(body.get("msg") or "Invalid device list response")
        return body["data_devices"]

    def recordings(self, *, is_trash: int = 0, limit: int = 99999) -> list[Recording]:
        body = self._request(
            "GET",
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": limit,
                "is_trash": is_trash,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
        return [Recording.from_api(d) for d in body.get("data_file_list") or []]

    def temp_url(self, file_id: str, *, opus: bool = False) -> str:
        body = self._request("GET", f"/file/temp-url/{file_id}", params={"is_opus": 1 if opus else 0})
        url = body.get("temp_url_opus") if opus else None
        return url or body.get("temp_url") or ""

    def file_detail(self, file_id: str) -> dict:
        return self._request("GET", f"/file/detail/{file_id}")

    def download(self, file_id: str, dest, *, opus: bool = False) -> int:
        """Stream a recording to `dest`. Returns bytes written."""
        url = self.temp_url(file_id, opus=opus)
        if not url:
            raise PlaudApiError(f"No download URL for {file_id}")
        written = 0
        with self._http.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
                    written += len(chunk)
        return written

    # ----------------------------------------------------------------- mutations

    def set_filename(self, file_id: str, filename: str) -> dict:
        return self._request("PATCH", f"/file/{file_id}", json={"filename": filename})

    def trash(self, file_id: str) -> dict:
        """Move a recording to Plaud's trash (reversible in the app for ~30 days).

        NOTE: this endpoint shape is inferred from the `is_trash` field on the
        listing response, not from a documented/observed call. `plaudctl prune`
        probes it on ONE file and verifies before it will touch anything else.
        """
        return self._request("PATCH", f"/file/{file_id}", json={"is_trash": True})
