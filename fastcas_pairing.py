"""Optional local FastCAS pairing; it never authorizes FastLab tasks."""
from __future__ import annotations

import os
import json
import secrets
import threading
import time
import uuid
from pathlib import Path


class FastCASPairing:
    def __init__(self, store, env=None):
        env = os.environ if env is None else env
        self.store = store
        self.issuer = env.get("FASTLAB_FASTCAS_ISSUER", "").strip().rstrip("/")
        self.client_id = env.get("FASTLAB_FASTCAS_CLIENT_ID", "").strip()
        if bool(self.issuer) != bool(self.client_id):
            raise ValueError("FastCAS 关联需同时配置 issuer 和公开客户端 ID")
        self.allow_loopback = env.get("FASTLAB_FASTCAS_ALLOW_LOOPBACK_HTTP") == "true"
        self.installation_id = store.get_setting("installation.id")
        if not self.installation_id:
            self.installation_id = str(uuid.uuid4())
            store.set_setting("installation.id", self.installation_id)
        self.lock = threading.RLock()
        self.pending = None
        self.csrf = secrets.token_urlsafe(32)
        self.credential_path = Path(store.path).parent / "fastcas-installation.json"
        self.next_status_check = 0.0
        self.next_revocation_attempt = 0.0

    @property
    def available(self):
        return bool(self.issuer and self.client_id)

    def _sdk(self):
        if not self.available:
            raise ValueError("此安装未配置 FastCAS 关联")
        from fastcas import Configuration, FastCAS

        class NoBrowserTransactions:
            def put(self, _):
                raise RuntimeError("设备关联不能启动回调登录")
            def take(self, _):
                raise RuntimeError("设备关联不能消费回调")

        return FastCAS(Configuration(self.issuer, self.client_id, self.issuer + "/device",
                                     allow_loopback_http=self.allow_loopback), NoBrowserTransactions())

    def status(self):
        with self.lock:
            self._sync_installation()
            pending = self.pending
            if pending and pending["expires_at"] <= time.time():
                self.pending = pending = None
            return {
                "available": self.available,
                "installation_id": self.installation_id,
                "link": self.store.get_setting("fastcas.link"),
                "pairing": ({key: pending[key] for key in ("user_code", "verification_uri", "verification_uri_complete", "expires_at", "candidate")}
                            if pending else None),
            }

    def start(self):
        with self.lock:
            self._flush_revocation(force=True)
            if self._credential() and self._credential().get("pending_revoke"):
                raise ValueError("上次解除关联尚未同步到 FastCAS，请联网后重试")
            if self.store.get_setting("fastcas.link"):
                raise ValueError("请先解除当前 FastCAS 关联")
            if self.pending and self.pending["expires_at"] > time.time():
                return self.status()["pairing"]
            sdk = self._sdk()
            try:
                result = sdk.device_authorize(["openid", "profile"])
            finally:
                sdk.close()
            expires = time.time() + min(max(int(result.get("expires_in", 0)), 1), 600)
            interval = min(max(int(result.get("interval", 5)), 5), 30)
            self.pending = {
                "device_code": result["device_code"], "user_code": result["user_code"],
                "verification_uri": result["verification_uri"], "expires_at": expires,
                "verification_uri_complete": result.get("verification_uri_complete") or result["verification_uri"],
                "interval": interval, "next_poll": time.monotonic() + interval,
                "candidate": None,
            }
            return self.status()["pairing"]

    def poll(self):
        with self.lock:
            pending = self.pending
            if not pending or pending["expires_at"] <= time.time():
                self.pending = None
                raise ValueError("配对已过期，请重新开始")
            if pending["candidate"]:
                return self.status()["pairing"]
            wait = pending["next_poll"] - time.monotonic()
            if wait > 0:
                return {"state": "pending", "retry_after": max(1, int(wait + 1))}
            sdk = self._sdk()
            try:
                from fastcas import FastCASError
                try:
                    result = sdk.poll_device(pending["device_code"])
                except FastCASError as exc:
                    if exc.code == "authorization_pending":
                        pending["next_poll"] = time.monotonic() + pending["interval"]
                        return {"state": "pending", "retry_after": pending["interval"]}
                    if exc.code == "slow_down":
                        pending["interval"] = min(pending["interval"] + 5, 30)
                        pending["next_poll"] = time.monotonic() + pending["interval"]
                        return {"state": "pending", "retry_after": pending["interval"]}
                    if exc.code in {"access_denied", "expired_token", "expired_device_code"}:
                        self.pending = None
                    raise
            finally:
                sdk.close()
            pending["candidate"] = result["identity"]
            pending["access_token"] = result["tokens"]["access_token"]
            return {"state": "confirm", "candidate": pending["candidate"]}

    def confirm(self):
        with self.lock:
            pending = self.pending
            if not pending or pending["expires_at"] <= time.time() or not pending["candidate"]:
                raise ValueError("尚无经过 FastCAS 验证的待确认身份")
            if self.store.get_setting("fastcas.link"):
                raise ValueError("已有 FastCAS 关联")
            candidate = pending["candidate"]
            sdk = self._sdk()
            try:
                registered = sdk.register_device_installation(pending["access_token"], self.installation_id)
            finally:
                sdk.close()
            installation = registered["installation"]
            credential = {"id": installation["id"], "management_secret": registered["management_secret"]}
            try:
                self._save_credential(credential)
            except Exception:
                sdk = self._sdk()
                try:
                    sdk.revoke_device_installation(credential["id"], credential["management_secret"])
                finally:
                    sdk.close()
                raise
            link = {"installation_id": self.installation_id, "issuer": candidate["issuer"],
                    "subject": candidate["subject"], "client_id": self.client_id,
                    "verified_at": int(time.time()), "center_record_id": credential["id"]}
            self.store.set_setting("fastcas.link", link)
            self.pending = None
            self.next_status_check = time.monotonic() + 15
            return link

    def unlink(self):
        with self.lock:
            credential = self._credential()
            if credential:
                credential["pending_revoke"] = True
                self._save_credential(credential)
            self.store.set_setting("fastcas.link", None)
            self.pending = None
            self._flush_revocation(force=True)
            return {"ok": True}

    def _credential(self):
        try:
            with self.credential_path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None

    def _save_credential(self, value):
        temporary = self.credential_path.with_name(self.credential_path.name + "." + secrets.token_hex(8))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.credential_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _flush_revocation(self, force=False):
        credential = self._credential()
        if not credential or not credential.get("pending_revoke"):
            return
        if not force and time.monotonic() < self.next_revocation_attempt:
            return
        self.next_revocation_attempt = time.monotonic() + 60
        try:
            sdk = self._sdk()
            try:
                sdk.revoke_device_installation(credential["id"], credential["management_secret"])
            finally:
                sdk.close()
        except Exception as exc:
            if getattr(exc, "status", None) in {401, 403, 404}:
                self.credential_path.unlink(missing_ok=True)  # The restored provider no longer recognizes this record.
            return  # Local unlink succeeds offline; retry network failures later.
        self.credential_path.unlink(missing_ok=True)

    def _sync_installation(self):
        self._flush_revocation()
        if not self.store.get_setting("fastcas.link") or time.monotonic() < self.next_status_check:
            return
        self.next_status_check = time.monotonic() + 15
        credential = self._credential()
        if not credential:
            return  # Preserve links created before central installation registration.
        try:
            sdk = self._sdk()
            try:
                result = sdk.device_installation_status(credential["id"], credential["management_secret"])
            finally:
                sdk.close()
        except Exception as exc:
            if getattr(exc, "status", None) in {401, 403, 404}:
                self.store.set_setting("fastcas.link", None)
                self.credential_path.unlink(missing_ok=True)
            return  # FastCAS downtime cannot stop local work.
        if result.get("revoked_at"):
            self.store.set_setting("fastcas.link", None)
            self.credential_path.unlink(missing_ok=True)
