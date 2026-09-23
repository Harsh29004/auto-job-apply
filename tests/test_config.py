from naukri_bot.config import ROOT, load_config


def test_env_overrides_yaml(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "JOB_KEYWORDS=python developer, react developer\n"
        "JOB_LOCATIONS=\n"
        "JOB_TITLE_EXCLUDE=senior, java\n"
        "JOB_MAX_MIN_EXPERIENCE=2\n"
        "MAX_APPLIES_PER_RUN=10\n", encoding="utf-8")
    for var in ("JOB_KEYWORDS", "JOB_LOCATIONS", "JOB_TITLE_EXCLUDE", "JOB_TITLE_INCLUDE",
                "JOB_MAX_MIN_EXPERIENCE", "MAX_APPLIES_PER_RUN", "JOB_MIN_SCORE"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(ROOT / "config.yaml", env_path=env)
    assert cfg.search["keywords"] == ["python developer", "react developer"]
    assert cfg.search["locations"] == []
    assert cfg.filters["title_exclude"] == ["senior", "java"]
    assert cfg.filters["max_min_experience"] == 2
    assert cfg.apply["max_applies_per_run"] == 10
    assert "python" in cfg.filters["title_include"]  # not in .env -> config.yaml value kept


def test_real_env_is_loaded():
    cfg = load_config()
    assert "machine learning engineer" in cfg.search["keywords"]
    assert "senior" in cfg.filters["title_exclude"]
