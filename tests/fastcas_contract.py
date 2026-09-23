"""Real FastCAS provider -> Python SDK -> local FastLab pairing contract."""
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from fastcas_pairing import FastCASPairing
from server import Store

issuer = os.environ["FASTCAS_CONTRACT_ISSUER"]
with tempfile.TemporaryDirectory(prefix="fastlab-cas-contract-") as directory:
    store = Store(Path(directory) / "local.db")
    pairing = FastCASPairing(store, {
        "FASTLAB_FASTCAS_ISSUER": issuer,
        "FASTLAB_FASTCAS_CLIENT_ID": "fastlab-device",
        "FASTLAB_FASTCAS_ALLOW_LOOPBACK_HTTP": "true",
    })
    started = pairing.start()
    assert started["user_code"] and started["verification_uri_complete"].startswith(issuer + "/device")
    assert pairing.poll()["state"] == "pending"
    with httpx.Client(base_url=issuer, follow_redirects=False) as browser:
        login = browser.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', login.text).group(1)
        signed_in = browser.post("/login", data={"csrf": csrf, "email": "alice@example.test",
            "password": "correct horse battery staple", "action": "login"}, headers={"Origin": issuer})
        assert signed_in.status_code == 303, signed_in.text
        page = browser.get(started["verification_uri_complete"])
        assert page.status_code == 200 and "FastLab desktop" in page.text, page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        denied = browser.post("/device", data={"csrf": csrf, "user_code": started["user_code"], "decision": "approve"},
                              headers={"Origin": "https://attacker.example"})
        assert denied.status_code == 403
        approved = browser.post("/device", data={"csrf": csrf, "user_code": started["user_code"], "decision": "approve"},
                                headers={"Origin": issuer})
        assert approved.status_code == 200, approved.text
        while time.monotonic() < pairing.pending["next_poll"]:
            time.sleep(.05)
        outcome = pairing.poll()
        assert outcome["state"] == "confirm", outcome
        assert not store.get_setting("fastcas.link")
        link = pairing.confirm()
        assert link["issuer"] == issuer and link["subject"] == os.environ["FASTCAS_CONTRACT_SUBJECT"]
        assert link["installation_id"] == pairing.installation_id
        assert FastCASPairing(store).installation_id == pairing.installation_id
        listed = browser.get("/api/v1/me/device-installations")
        assert listed.status_code == 200, listed.text
        current = [item for item in listed.json()["installations"] if item["id"] == link["center_record_id"]]
        assert len(current) == 1 and current[0]["installation_id"] == pairing.installation_id and not current[0]["revoked_at"]
        pairing.unlink()
        assert not store.get_setting("fastcas.link")
        listed = browser.get("/api/v1/me/device-installations")
        assert next(item for item in listed.json()["installations"] if item["id"] == link["center_record_id"])["revoked_at"]
print("FastLab device contract passed: browser approval, SDK verification, central installation record, local confirmation and unlink")
