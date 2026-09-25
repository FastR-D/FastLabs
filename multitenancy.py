"""Authentication, encrypted tenant metadata, and isolated container launching."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import cookies
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


PROVIDERS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def tenant_key(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


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
        return tenant_key(self.account_id)


class ResearchAuthenticator:
    def __init__(self, api_url: str, cookie_name="fr_session", ttl=30):
        self.api_url = str(api_url).strip().rstrip("/")
        self.cookie_name = str(cookie_name or "fr_session")
        self.ttl = max(0, min(30, int(ttl)))
        self.cache = {}
        self.lock = threading.Lock()

    def check(self):
        request = urllib.request.Request(self.api_url + "/api/health", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise ValueError("无法连接 FastResearch 健康检查。") from exc
        if not payload.get("ok"):
            raise ValueError("FastResearch 健康检查未通过。")

    def authenticate(self, cookie_header: str) -> Principal | None:
        token = parse_cookie(cookie_header, self.cookie_name)
        if not token:
            return None
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(hashlib.sha256(token.encode()).digest())
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
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None
        account_id = str(payload.get("accountId") or "").strip()
        person = str(payload.get("person") or "").strip()
        if not account_id:
            return None
        principal = Principal(account_id, person or "FastResearch 用户", token)
        with self.lock:
            self.cache[hashlib.sha256(token.encode()).digest()] = (now + self.ttl, principal)
        return principal


class TenantRegistry:
    SCHEMA_VERSION = "2"

    def __init__(self, database: Path, tenant_root: Path, master_key: str):
        self.database = Path(database).resolve()
        self.tenant_root = Path(tenant_root).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.tenant_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            raw = str(master_key or "").encode("ascii")
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) != 32:
                raise ValueError
            self.cipher = Fernet(raw)
        except (ValueError, TypeError, InvalidToken) as exc:
            raise ValueError(
                "FASTLAB_CREDENTIAL_MASTER_KEY 必须是 Fernet 兼容的 32 字节 URL-safe base64 密钥。"
            ) from exc
        self._initialize()
        os.chmod(self.database, 0o600)

    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _initialize(self):
        with self.connect() as db:
            existing = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {row["name"] for row in existing}
            if names and "metadata" not in names:
                raise ValueError("FastLab v2 数据库 Schema 不匹配；不会覆盖已有数据库。")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenants (
                    account_id TEXT PRIMARY KEY,
                    tenant_key TEXT NOT NULL UNIQUE,
                    person TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    account_id TEXT NOT NULL REFERENCES tenants(account_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    preview TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(account_id, provider)
                );
                """
            )
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and version["value"] != self.SCHEMA_VERSION:
                raise ValueError("FastLab v2 数据库版本不受支持。")
            db.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)",
                (self.SCHEMA_VERSION,),
            )

    def ensure_tenant(self, principal: Principal) -> dict:
        key = principal.tenant_key
        with self.connect() as db:
            db.execute(
                "INSERT INTO tenants(account_id,tenant_key,person) VALUES(?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET person=excluded.person,updated_at=CURRENT_TIMESTAMP",
                (principal.account_id, key, principal.person),
            )
        root = (self.tenant_root / key).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        for name in ("repositories", "tasks", "worktrees", "runtime"):
            directory = root / name
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        return {"accountId": principal.account_id, "tenantKey": key, "person": principal.person, "root": root}

    def credential_status(self, account_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT provider,preview,updated_at FROM credentials WHERE account_id=?",
                (account_id,),
            ).fetchall()
        found = {row["provider"]: row for row in rows}
        return {
            provider: {
                "configured": provider in found,
                "preview": found[provider]["preview"] if provider in found else "",
                "updatedAt": found[provider]["updated_at"] if provider in found else None,
            }
            for provider in PROVIDERS
        }

    def set_credential(self, account_id: str, provider: str, value: str):
        provider = str(provider).lower()
        value = str(value or "").strip()
        if provider not in PROVIDERS:
            raise ValueError("不支持的凭证提供方。")
        if not value or len(value) > 4096 or any(char in value for char in "\r\n\x00"):
            raise ValueError("API Key 格式无效。")
        preview = value[:4] + "…" + value[-4:] if len(value) >= 12 else "已配置"
        encrypted = self.cipher.encrypt(value.encode("utf-8"))
        with self.connect() as db:
            db.execute(
                "INSERT INTO credentials(account_id,provider,ciphertext,preview) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id,provider) DO UPDATE SET ciphertext=excluded.ciphertext," 
                "preview=excluded.preview,updated_at=CURRENT_TIMESTAMP",
                (account_id, provider, encrypted, preview),
            )
        return self.credential_status(account_id)[provider]

    def delete_credential(self, account_id: str, provider: str):
        if provider not in PROVIDERS:
            raise ValueError("不支持的凭证提供方。")
        with self.connect() as db:
            db.execute("DELETE FROM credentials WHERE account_id=? AND provider=?", (account_id, provider))

    def credential(self, account_id: str, provider: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT ciphertext FROM credentials WHERE account_id=? AND provider=?",
                (account_id, provider),
            ).fetchone()
        if not row:
            return ""
        try:
            return self.cipher.decrypt(bytes(row["ciphertext"])).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError("租户凭证无法解密；请检查主密钥。") from exc

    def redact(self, account_id: str, value) -> str:
        text = str(value or "")
        for provider in PROVIDERS:
            secret = self.credential(account_id, provider)
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return redact_secret(text)


class DockerCommandRunner:
    """Turn an Agent CLI command into a hardened one-shot Docker command."""

    def __init__(self, image: str, tenant_root: Path, app_root: Path, credential_provider,
                 cpus="2", memory="4g", pids=256):
        self.image = str(image or "").strip()
        if not self.image:
            raise ValueError("FASTLAB_WORKER_IMAGE 不能为空。")
        self.tenant_root = Path(tenant_root).resolve()
        self.app_root = Path(app_root).resolve()
        self.credential_provider = credential_provider
        self.cpus, self.memory, self.pids = str(cpus), str(memory), int(pids)
        self.active = {}
        self.lock = threading.Lock()

    def _map(self, value: str, cwd: Path) -> str:
        value = str(value)
        if not Path(value).is_absolute():
            return value
        mappings = ((cwd, Path("/workspace")), (self.app_root, Path("/fastlab")),
                    (self.tenant_root / "runtime", Path("/runtime")))
        for source, target in mappings:
            try:
                relative = Path(value).resolve().relative_to(source.resolve())
                return str(target / relative)
            except (ValueError, OSError):
                continue
        return value

    def wrap(self, command, cwd, writable, executor, key="run"):
        cwd = Path(cwd).resolve()
        try:
            cwd.relative_to(self.tenant_root)
        except ValueError as exc:
            raise ValueError("Agent 工作目录越出租户空间。") from exc
        provider = "openai" if executor == "codex" else "anthropic"
        secret = self.credential_provider(provider)
        if not secret:
            raise ValueError("请先配置 %s API Key。" % provider.title())
        mapped = [self._map(item, cwd) for item in command]
        executor_home = self.tenant_root / "runtime" / executor
        executor_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home = "/runtime/%s" % executor
        uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65532
        gid = os.getgid() if hasattr(os, "getgid") and os.getgid() else 65532
        name = "fastlab-%s-%s" % (
            self.tenant_root.name[:10], hashlib.sha256(str(key).encode()).hexdigest()[:16]
        )
        with self.lock:
            self.active[str(key)] = name
        docker = [
            "docker", "run", "--rm", "--name", name, "--init", "--read-only",
            "--log-driver=none",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit", str(self.pids), "--cpus", self.cpus, "--memory", self.memory,
            "--user", "%s:%s" % (uid, gid), "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            "--workdir", "/workspace",
            "--mount", "type=bind,src=%s,dst=/workspace,%s" % (cwd, "rw" if writable else "readonly"),
            "--mount", "type=bind,src=%s,dst=/runtime,rw" % (self.tenant_root / "runtime"),
            "--mount", "type=bind,src=%s,dst=/fastlab/schemas,readonly" % (self.app_root / "schemas"),
            "--mount", "type=bind,src=%s,dst=/fastlab/skills,readonly" % (self.app_root / "skills"),
            "--env", PROVIDERS[provider], "--env", "HOME=" + home,
            "--env", "CODEX_HOME=" + home + "/.codex",
        ]
        try:
            common = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=5, check=True,
            ).stdout.strip()
            common_path = Path(common).resolve()
            try:
                common_path.relative_to(cwd)
            except ValueError:
                try:
                    common_path.relative_to(self.tenant_root / "repositories")
                except ValueError as exc:
                    raise ValueError("Git 元数据越出租户仓库空间。") from exc
                docker.extend([
                    "--mount", "type=bind,src=%s,dst=%s,rw" % (common_path, common_path)
                ])
        except subprocess.CalledProcessError:
            pass
        docker += [self.image] + mapped
        environment = {PROVIDERS[provider]: secret, "PATH": os.environ.get("PATH", "")}
        return docker, str(cwd), environment

    def interrupt(self, key):
        with self.lock:
            name = self.active.get(str(key))
        if not name:
            return False
        subprocess.run(
            ["docker", "kill", name], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        return True

    def finished(self, key):
        with self.lock:
            name = self.active.pop(str(key), None)
        if name:
            subprocess.run(
                ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
            )

    def check(self):
        result = subprocess.run(
            ["docker", "image", "inspect", self.image], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=10, check=False,
        )
        if result.returncode:
            raise ValueError("找不到 FastLab Worker 镜像：%s" % self.image)


def safe_public_path(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("FASTLAB_PUBLIC_PATH 必须是绝对 URL 路径。")
    return value.rstrip("/")


def redact_secret(value: str) -> str:
    return re.sub(r"(?:sk|key)-[A-Za-z0-9_-]{8,}", "[REDACTED]", str(value or ""))
