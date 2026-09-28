"""Encrypted tenant metadata, credentials, and directory ownership."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .integrations.fastresearch import (
    Principal as Principal,
    ResearchAuthenticator as ResearchAuthenticator,
    parse_cookie as parse_cookie,
)

PROVIDERS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def tenant_key(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


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

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
                CREATE TABLE IF NOT EXISTS credential_modes (
                    account_id TEXT NOT NULL REFERENCES tenants(account_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'cli',
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
        for name in ("repositories", "tasks", "worktrees", "runtime", "skills"):
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
        with self.connect() as db:
            mode_rows = db.execute(
                "SELECT provider,mode FROM credential_modes WHERE account_id=?",
                (account_id,),
            ).fetchall()
        modes = {row["provider"]: row["mode"] for row in mode_rows}
        return {
            provider: {
                "configured": provider in found,
                "preview": found[provider]["preview"] if provider in found else "",
                "updatedAt": found[provider]["updated_at"] if provider in found else None,
                "mode": modes.get(provider, "cli"),
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
            db.execute(
                "INSERT INTO credential_modes(account_id,provider,mode) VALUES(?,?,'api_key') "
                "ON CONFLICT(account_id,provider) DO UPDATE SET mode='api_key',"
                "updated_at=CURRENT_TIMESTAMP",
                (account_id, provider),
            )
        return self.credential_status(account_id)[provider]

    def set_credential_mode(self, account_id: str, provider: str, mode: str):
        provider = str(provider or "").lower()
        mode = str(mode or "").lower()
        if provider not in PROVIDERS or mode not in {"cli", "api_key"}:
            raise ValueError("认证方式无效。")
        if mode == "api_key" and not self.credential(account_id, provider):
            raise ValueError("选择 API Key 前请先保存对应密钥。")
        with self.connect() as db:
            db.execute(
                "INSERT INTO credential_modes(account_id,provider,mode) VALUES(?,?,?) "
                "ON CONFLICT(account_id,provider) DO UPDATE SET mode=excluded.mode,"
                "updated_at=CURRENT_TIMESTAMP",
                (account_id, provider, mode),
            )
        return self.credential_status(account_id)[provider]

    def credential_mode(self, account_id: str, provider: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError("不支持的凭证提供方。")
        with self.connect() as db:
            row = db.execute(
                "SELECT mode FROM credential_modes WHERE account_id=? AND provider=?",
                (account_id, provider),
            ).fetchone()
        return str(row["mode"] if row else "cli")

    def effective_credential(self, account_id: str, provider: str) -> str:
        return self.credential(account_id, provider) if self.credential_mode(
            account_id, provider
        ) == "api_key" else ""

    def delete_credential(self, account_id: str, provider: str):
        if provider not in PROVIDERS:
            raise ValueError("不支持的凭证提供方。")
        with self.connect() as db:
            db.execute("DELETE FROM credentials WHERE account_id=? AND provider=?", (account_id, provider))
            db.execute(
                "INSERT INTO credential_modes(account_id,provider,mode) VALUES(?,?,'cli') "
                "ON CONFLICT(account_id,provider) DO UPDATE SET mode='cli',"
                "updated_at=CURRENT_TIMESTAMP",
                (account_id, provider),
            )

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


def safe_public_path(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("FASTLAB_PUBLIC_PATH 必须是绝对 URL 路径。")
    return value.rstrip("/")


def redact_secret(value: str) -> str:
    return re.sub(r"(?:sk|key)-[A-Za-z0-9_-]{8,}", "[REDACTED]", str(value or ""))
