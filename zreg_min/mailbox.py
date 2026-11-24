"""Cloud Mail (email.jijingxing.me) API client.

Verified endpoints (2026-11-07):
  POST /api/login              {email,password}          -> {code:200,data:{token}}
  GET  /api/account/list       ?accountId&size&lastSort  -> accounts[]
  POST /api/account/add        {email}                   -> {accountId,...}
  GET  /api/allEmail/list      ?accountId&size&lastSort&type=receive -> {list:[...]}
  GET  /api/email/list         ?accountId&allReceive&emailId&timeSort&size&type&full
Authorization header: raw JWT token (no Bearer prefix).
"""
from __future__ import annotations

import re
import time
import urllib.request
import urllib.parse
import json
import ssl
import urllib.error

BASE = "https://email.jijingxing.me"
import os
ADMIN_EMAIL = os.environ.get("CLOUDMAIL_EMAIL", "")      # secrets，不落代码
ADMIN_PASSWORD = os.environ.get("CLOUDMAIL_PASSWORD", "")


class CloudMailError(Exception):
    pass


class CloudMail:
    def __init__(self, base: str = BASE, email: str = ADMIN_EMAIL,
                 password: str = ADMIN_PASSWORD, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token
        if not token:
            self.login(email, password)

    # ------------------------------------------------------------------ http
    def _req(self, method: str, path: str, body: dict | None = None,
             params: dict | None = None, timeout: int = 20) -> dict:
        url = f"{self.base}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                          for k, v in params.items() if v is not None)
            url = f"{url}?{qs}"
        data = None
        headers = {"Accept": "application/json",
                   "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/131.0.0.0 Safari/537.36")}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # Only retry reads: a dropped POST response may already have committed.
        attempts = 3 if method.upper() == 'GET' else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    payload = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                raise CloudMailError(f"{method} {path} HTTP {e.code}") from e
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as e:
                if attempt + 1 == attempts:
                    reason = getattr(e, 'reason', e)
                    raise CloudMailError(
                        f"{method} {path} transport failure ({type(reason).__name__}); "
                        "check mailbox TLS/proxy connection"
                    ) from e
                time.sleep(0.5 * (attempt + 1))
        if payload.get("code") != 200:
            raise CloudMailError(f"{method} {path}: {payload}")
        return payload.get("data")

    # ------------------------------------------------------------------ api
    def login(self, email: str, password: str) -> str:
        d = self._req("POST", "/api/login", {"email": email, "password": password})
        self.token = d["token"]
        return self.token

    def accounts(self) -> list[dict]:
        return self._req("GET", "/api/account/list",
                         params={"accountId": 0, "size": 100, "lastSort": 0})

    def add_account(self, email: str) -> dict:
        return self._req("POST", "/api/account/add", {"email": email})

    def receive_list(self, account_id: int, size: int = 10) -> list[dict]:
        d = self._req("GET", "/api/allEmail/list",
                      params={"accountId": account_id, "size": size,
                              "lastSort": 0, "type": "receive"})
        return d.get("list") or []

    def wait_zai_verify(self, account_id: int, email: str | None = None,
                        since_ts: float | None = None,
                        timeout: float = 240.0, poll: float = 6.0) -> dict:
        """Poll for the newest Z.ai email-verification mail TO this address.

        `email`/`since_ts` are optional filters (mailbox accounts are already
        scoped per-address by `account_id`).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                mails = self.receive_list(account_id, size=10)
            except Exception:
                mails = []
            for m in mails:
                subj = (m.get("subject") or "").lower()
                if m.get("sendEmail") != "noreply@email.z.ai":
                    continue
                if email and m.get("toEmail", "").lower() != email.lower():
                    continue
                if ("verify" not in subj) and ("验证" not in subj):
                    continue
                return m
            time.sleep(poll)
        raise CloudMailError("no Z.ai verify mail within timeout")
