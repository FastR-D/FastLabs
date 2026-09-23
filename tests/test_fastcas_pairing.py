import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastcas_pairing import FastCASPairing
from server import FastLabHandler, Store, ThreadingHTTPServer


class FakeSDK:
    records = {}
    def __init__(self, *_args, **_kwargs):
        self.polls = 0
    def device_authorize(self, scopes):
        assert scopes == ["openid", "profile"]
        return {"device_code": "opaque", "user_code": "BCDF-GHJK", "verification_uri": "http://127.0.0.1:8900/device",
                "verification_uri_complete": "http://127.0.0.1:8900/device?user_code=BCDF-GHJK", "expires_in": 600, "interval": 5}
    def poll_device(self, code):
        assert code == "opaque"
        return {"identity": {"issuer": "http://127.0.0.1:8900", "subject": "subject-a"}, "tokens": {"access_token": "discard"}}
    def register_device_installation(self, token, installation_id):
        assert token == "discard"
        self.records[installation_id] = {"id": "record-a", "installation_id": installation_id, "revoked_at": None}
        return {"installation": self.records[installation_id], "management_secret": "secret-only-on-disk"}
    def device_installation_status(self, record_id, secret):
        assert record_id == "record-a" and secret == "secret-only-on-disk"
        return next(iter(self.records.values()))
    def revoke_device_installation(self, record_id, secret):
        assert record_id == "record-a" and secret == "secret-only-on-disk"
        next(iter(self.records.values()))["revoked_at"] = "now"
    def close(self):
        pass


class ProviderRejected(Exception):
    status = 401


class PairingTests(unittest.TestCase):
    def setUp(self):
        FakeSDK.records.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "local.db")
        self.env = {"FASTLAB_FASTCAS_ISSUER": "http://127.0.0.1:8900", "FASTLAB_FASTCAS_CLIENT_ID": "fastlab",
                    "FASTLAB_FASTCAS_ALLOW_LOOPBACK_HTTP": "true"}
        self.pairing = FastCASPairing(self.store, self.env)

    def tearDown(self):
        self.temp.cleanup()

    def test_stable_installation_and_explicit_local_confirmation(self):
        fake_module = types.SimpleNamespace(FastCASError=type("FastCASError", (Exception,), {}))
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.dict(sys.modules, {"fastcas": fake_module}):
            started = self.pairing.start()
            self.assertEqual(started["user_code"], "BCDF-GHJK")
            with self.assertRaises(ValueError):
                self.pairing.confirm()
            self.assertEqual(self.pairing.poll()["state"], "pending")
            self.pairing.pending["next_poll"] = time.monotonic() - 1
            self.assertEqual(self.pairing.poll()["state"], "confirm")
            self.assertIsNone(self.store.get_setting("fastcas.link"))
            link = self.pairing.confirm()
            self.assertEqual(link["subject"], "subject-a")
            self.assertEqual(link["installation_id"], self.pairing.installation_id)
            self.assertEqual(link["center_record_id"], "record-a")
            self.assertNotIn("secret-only-on-disk", json.dumps(self.pairing.status()))
            self.assertEqual(self.pairing.credential_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(FastCASPairing(self.store, self.env).installation_id, link["installation_id"])
            self.assertTrue(self.pairing.unlink()["ok"])
            self.assertIsNone(self.store.get_setting("fastcas.link"))
            self.assertFalse(self.pairing.credential_path.exists())

    def test_center_revoke_clears_local_link_without_touching_installation(self):
        fake_module = types.SimpleNamespace(FastCASError=type("FastCASError", (Exception,), {}))
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.dict(sys.modules, {"fastcas": fake_module}):
            self.pairing.start()
            self.pairing.pending["next_poll"] = time.monotonic() - 1
            self.pairing.poll()
            self.pairing.confirm()
            FakeSDK.records[self.pairing.installation_id]["revoked_at"] = "now"
            self.pairing.next_status_check = 0
            self.assertIsNone(self.pairing.status()["link"])
            self.assertFalse(self.pairing.credential_path.exists())
            self.assertEqual(self.store.get_setting("installation.id"), self.pairing.installation_id)

    def test_offline_unlink_queues_center_revocation(self):
        fake_module = types.SimpleNamespace(FastCASError=type("FastCASError", (Exception,), {}))
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.dict(sys.modules, {"fastcas": fake_module}):
            self.pairing.start()
            self.pairing.pending["next_poll"] = time.monotonic() - 1
            self.pairing.poll()
            self.pairing.confirm()
        with patch.object(self.pairing, "_sdk", side_effect=OSError("offline")):
            self.assertTrue(self.pairing.unlink()["ok"])
            self.assertIsNone(self.store.get_setting("fastcas.link"))
            self.assertTrue(self.pairing._credential()["pending_revoke"])
            with self.assertRaisesRegex(ValueError, "尚未同步"):
                self.pairing.start()
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()):
            self.pairing.start()
            self.assertFalse(self.pairing.credential_path.exists())
            self.assertEqual(FakeSDK.records[self.pairing.installation_id]["revoked_at"], "now")

    def test_legacy_local_link_survives_upgrade(self):
        legacy = {"installation_id": self.pairing.installation_id, "issuer": self.pairing.issuer,
                  "subject": "subject-a", "client_id": self.pairing.client_id}
        self.store.set_setting("fastcas.link", legacy)
        self.assertEqual(self.pairing.status()["link"], legacy)
        self.assertTrue(self.pairing.unlink()["ok"])
        self.assertIsNone(self.pairing.status()["link"])

    def test_restored_provider_missing_installation_clears_local_link(self):
        fake_module = types.SimpleNamespace(FastCASError=type("FastCASError", (Exception,), {}))
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.dict(sys.modules, {"fastcas": fake_module}):
            self.pairing.start()
            self.pairing.pending["next_poll"] = time.monotonic() - 1
            self.pairing.poll()
            self.pairing.confirm()
            self.pairing.next_status_check = 0
            with patch.object(FakeSDK, "device_installation_status", side_effect=ProviderRejected()):
                self.assertIsNone(self.pairing.status()["link"])
            self.assertFalse(self.pairing.credential_path.exists())
            self.assertEqual(self.store.get_setting("installation.id"), self.pairing.installation_id)

    def test_restored_provider_missing_pending_revoke_allows_repairing(self):
        fake_module = types.SimpleNamespace(FastCASError=type("FastCASError", (Exception,), {}))
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.dict(sys.modules, {"fastcas": fake_module}):
            self.pairing.start()
            self.pairing.pending["next_poll"] = time.monotonic() - 1
            self.pairing.poll()
            self.pairing.confirm()
        with patch.object(self.pairing, "_sdk", side_effect=OSError("offline")):
            self.pairing.unlink()
        with patch.object(self.pairing, "_sdk", return_value=FakeSDK()), patch.object(FakeSDK, "revoke_device_installation", side_effect=ProviderRejected()):
            self.assertTrue(self.pairing.start()["user_code"])
            self.assertFalse(self.pairing.credential_path.exists())

    def test_local_host_origin_and_csrf_required(self):
        fake = types.SimpleNamespace(fastcas_pairing=self.pairing)
        FastLabHandler.app = fake
        server = ThreadingHTTPServer(("127.0.0.1", 0), FastLabHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        try:
            status = json.loads(opener.open(origin + "/api/fastcas/status").read())
            self.assertTrue(status["available"])
            self.assertEqual(status["installation_id"], self.pairing.installation_id)
            def request(headers):
                return Request(origin + "/api/fastcas/pair", data=b"{}", headers=headers, method="POST")
            with self.assertRaises(HTTPError) as error:
                opener.open(request({"Origin": "https://evil.example", "X-CSRF-Token": status["csrf"]}))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                opener.open(request({"Origin": origin}))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(origin + "/api/fastcas/status", headers={"Host": f"evil.example:{server.server_port}"}))
            self.assertEqual(error.exception.code, 403)
            with patch.object(self.pairing, "_sdk", return_value=FakeSDK()):
                result = json.loads(opener.open(request({"Origin": origin, "X-CSRF-Token": status["csrf"]})).read())
            self.assertEqual(result["user_code"], "BCDF-GHJK")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
