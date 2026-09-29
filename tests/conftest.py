import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naukri_bot.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def answerer(cfg):
    from naukri_bot.answers import Answerer
    profile = dict(cfg.profile, current_ctc_lpa=2.4, expected_ctc_lpa=4)
    return Answerer(profile, cfg.skills, {})


@pytest.fixture(scope="session")
def server():
    """Serves tests/fixtures on a local port (browser tests never touch real sites)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "tests" / "fixtures"))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
