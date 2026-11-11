"""Build local browser assets from FastUI's fixed release; no npm at runtime."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

VERSION = "0.1.2"
SHA256 = "c3dabbad16980242f66db07619b161889d0ee2492daaa4aaddee1583bc7bcd0b"
URL = "https://registry.npmjs.org/@fastrd/ui-foundation/-/ui-foundation-0.1.2.tgz"


def supply(artifact=None):
    payload = Path(artifact).read_bytes() if artifact else urlopen(URL, timeout=60).read()
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise ValueError("FastUI foundation SHA-256 mismatch")
    destination = Path(__file__).resolve().parent / "fastui"
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for name in ("tokens.css", "controls.css", "color-mode.js"):
            member = archive.getmember(f"package/dist/{name}")
            if not member.isfile():
                raise ValueError(f"Not a regular package asset: {name}")
            content = archive.extractfile(member).read()
            (destination / name).write_bytes(content)
            files[name] = hashlib.sha256(content).hexdigest()
        for name in ("LICENSE", "NOTICE.md"):
            (destination / name).write_bytes(archive.extractfile(f"package/{name}").read())
    (destination / "provenance.json").write_text(json.dumps({
        "package": "@fastrd/ui-foundation", "version": VERSION,
        "url": URL, "sha256": SHA256, "files": files,
        "generated_by": "python web/supply_fastui.py",
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact")
    supply(parser.parse_args().artifact)
