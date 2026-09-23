import sys
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
