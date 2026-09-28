"""FastResearch session-validation contract used by FastLab."""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import cookies


def parse_cookie(header: str, name: str) -> str:
    jar = cookies.SimpleCookie()
    try:
        jar.load(header or "")
    except cookies.CookieError:
        return ""
    item = jar.get(name)
    return (item.value or "").strip() if item else ""


@dataclass(frozen=True)
class Principal:
    account_id: str
    person: str
    token: str

    @property
    def tenant_key(self) -> str:
        return hashlib.sha256(self.account_id.encode("utf-8")).hexdigest()


class ResearchAuthenticator:
    """Validate FastResearch cookies and cache successful principals briefly."""

    def __init__(self, api_url: str, cookie_name="fr_session", ttl=30):
        self.api_url = str(api_url).strip().rstrip("/")
        self.cookie_name = str(cookie_name or "fr_session")
        self.ttl = max(0, min(30, int(ttl)))
        self.cache = {}
        self.lock = threading.Lock()

    def check(self):
        request = urllib.request.Request(
            self.api_url + "/api/health", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise ValueError("无法连接 FastResearch 健康检查。") from exc
        if not payload.get("ok"):
            raise ValueError("FastResearch 健康检查未通过。")

    def authenticate(self, cookie_header: str) -> Principal | None:
        token = parse_cookie(cookie_header, self.cookie_name)
        if not token:
            return None
        now = time.monotonic()
        cache_key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            cached = self.cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        request = urllib.request.Request(
            self.api_url + "/api/content/me",
            headers={
                "Authorization": "Bearer " + token,
                "Cookie": "%s=%s" % (self.cookie_name, token),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ):
            return None
        account_id = str(payload.get("accountId") or "").strip()
        person = str(payload.get("person") or "").strip()
        if not account_id:
            return None
        principal = Principal(account_id, person or "FastResearch 用户", token)
        with self.lock:
            self.cache[cache_key] = (now + self.ttl, principal)
        return principal


__all__ = ["Principal", "ResearchAuthenticator", "parse_cookie"]
