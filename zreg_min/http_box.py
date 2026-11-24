"""Minimal HTTP helpers for box-local claim (extracted from zreg/http_flow.py).
stdlib only：UA / http_json / oauth_http，行为与 VPS 版一致。"""
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log_default(msg):
    print(msg, flush=True)


_OP = None


def get_opener():
    global _OP
    if _OP is None:
        v = os.environ.get("ZREG_HTTP_PROXY_URL", "").strip()
        if v and v.lower() not in ("direct", "tunnel"):
            _OP = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": v, "https": v}))
        else:
            _OP = urllib.request.build_opener()
        _OP.addheaders = [("User-Agent", UA)]
    return _OP


def _parse(txt):
    try:
        return json.loads(txt)
    except Exception:
        return {"_raw": txt[:500]}


def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None, timeout: int = 30,
              form: dict | None = None, opener=None):
    """Returns (status, parsed_json_or_text, resp_url)."""
    data = None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    op = opener or get_opener()
    try:
        with op.open(req, timeout=timeout) as r:
            txt = r.read().decode(errors="replace")
            return r.status, _parse(txt), r.geturl()
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        return e.code, _parse(txt), url
    except Exception as e:
        return 0, {"_err": str(e)}, url


def oauth_http(chat_jwt: str, log=log_default) -> dict | None:
    """OAuth via pure HTTP; returns zcode jwt bundle or None.
    (copied from zreg/http_flow.py:508 — stdlib only)"""
    poll_token = secrets.token_hex(32)
    status, resp, _ = http_json(
        "POST", "https://zcode.z.ai/api/v1/oauth/cli/init",
        body={"provider": "zai"},
        headers={"Authorization": f"Bearer {poll_token}"})
    if status != 200 or resp.get("code") not in (0, "0"):
        log(f"oauth init failed: {status} {str(resp)[:200]}")
        return None
    data = resp.get("data") or {}
    flow_id = data.get("flow_id")
    authorize_url = data.get("authorize_url")
    poll_iv = float(data.get("poll_interval_sec", 2))
    expires_at = float(data.get("expires_at", time.time() + 280))
    log(f"oauth flow {str(flow_id)[:10]}...")

    q = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
    form = {
        "client_id": q.get("client_id", [""])[0],
        "redirect_uri": q.get("redirect_uri", [""])[0],
        "state": q.get("state", [""])[0],
        "response_type": q.get("response_type", [""])[0],
        "action": "approve",
    }
    if not form["client_id"] or not form["redirect_uri"]:
        log(f"authorize url parse failed: {authorize_url[:120]}")
        return None

    status, resp, final_url = http_json(
        "POST", "https://chat.z.ai/api/oauth/authorize",
        form=form,
        headers={"Authorization": f"Bearer {chat_jwt}",
                 "Cookie": f"token={chat_jwt}",
                 "Origin": "https://chat.z.ai",
                 "Referer": "https://chat.z.ai/auth/oauth/authorize"},
        timeout=40)
    log(f"authorize approve -> {status} final={final_url[:110]} "
        f"body={str(resp)[:120]}")
    if status not in (200, 302, 307):
        return None

    redirect = None
    if isinstance(resp, dict):
        redirect = resp.get("redirect_url")
    if redirect:
        st2, resp2, url2 = http_json("GET", redirect, timeout=30)
        log(f"callback GET -> {st2} final={url2[:110]} "
            f"body={str(resp2)[:100]}")
        if st2 != 200:
            return None

    deadline = min(expires_at - 5, time.time() + 180)
    while time.time() < deadline:
        status, resp, _ = http_json(
            "GET", f"https://zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}",
            headers={"Authorization": f"Bearer {poll_token}"})
        d = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        st = d.get("status")
        if st == "ready":
            zai = d.get("zai") or {}
            return {"zcode_jwt": str(d.get("token") or ""),
                    "zai_access_token": str(zai.get("access_token")
                                            or zai.get("accessToken") or ""),
                    "user": d.get("user") or {}}
        if st == "failed":
            log(f"oauth failed: {d}")
            return None
        time.sleep(max(1.0, poll_iv))
    log("oauth poll timeout")
    return None
