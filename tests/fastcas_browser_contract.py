"""Real Chrome -> FastLab local page -> FastCAS device approval and local confirmation."""
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fake_adapter import FakeAgentAdapter
from server import FastLab, FastLabHandler, ThreadingHTTPServer

issuer = os.environ['FASTCAS_CONTRACT_ISSUER']
origin = os.environ['FASTCAS_CONTRACT_LABS_ORIGIN']
root = Path(__file__).resolve().parents[1]
os.environ.update(FASTLAB_FASTCAS_ISSUER=issuer, FASTLAB_FASTCAS_CLIENT_ID='fastlab-device',
                  FASTLAB_FASTCAS_ALLOW_LOOPBACK_HTTP='true')

with tempfile.TemporaryDirectory(prefix='fastlab-browser-') as temp:
    app = FastLab(root, Path(temp) / 'data', agent_adapter=FakeAgentAdapter(), claude_bin='/nonexistent')
    FastLabHandler.app = app
    server = ThreadingHTTPServer(('127.0.0.1', urlparse(origin).port), FastLabHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(origin + '/api/fastcas/status', timeout=.5).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(.05)
        else:
            raise AssertionError('FastLab browser server startup timeout')
        subprocess.run(['node', str(root.parent / 'FastCAS' / 'web' / 'tests' / 'fastlabs_contract.mjs')],
                       env=os.environ, check=True, timeout=70)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.shutdown()
