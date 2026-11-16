from pathlib import Path

import pytest

from stock_terminal.config import DEFAULTS, ConfigError, Keys, deep_merge, load_config, load_keys

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    assert cfg == DEFAULTS
    assert cfg["top_strip"]["fred"]["10Y"] == "DGS10"


def test_override_merges_nested(tmp_path):
    (tmp_path / "c.yaml").write_text("refresh_seconds: 30\nalpaca:\n  calls_per_minute: 10\n", encoding="utf-8")
    cfg = load_config(tmp_path / "c.yaml")
    assert cfg["refresh_seconds"] == 30
    assert cfg["alpaca"]["calls_per_minute"] == 10
    assert cfg["alpaca"]["feed"] == "iex"  # untouched default survives the merge
    assert cfg["cache"]["ttl_seconds"]["fred"] == 21600


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    out = deep_merge(base, {"a": {"c": 2}})
    assert out == {"a": {"b": 1, "c": 2}}
    assert base == {"a": {"b": 1}}


@pytest.mark.parametrize(
    "text",
    [
        "refresh_seconds: -5\n",
        "refresh_seconds: 0\n",
        "alpaca:\n  feed: nope\n",
        "top_strip:\n  equities: SPY\n",
        "cache:\n  ttl_seconds:\n    fred: 0\n",
        "- just\n- a list\n",
    ],
)
def test_bad_config_raises(tmp_path, text):
    (tmp_path / "c.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "c.yaml")


def test_shipped_config_yaml_is_valid():
    cfg = load_config(REPO_ROOT / "config.yaml")
    assert cfg["top_strip"]["equities"] == ["SPY", "QQQ"]
    assert cfg["top_strip"]["fred"] == {"VIX": "VIXCLS", "10Y": "DGS10"}


def test_keys_repr_never_shows_values():
    keys = Keys(alpaca_key_id="SECRET-ID", alpaca_secret="SECRET-KEY", fred="SECRET-FRED")
    for text in (repr(keys), str(keys)):
        assert "SECRET" not in text
        assert "alpaca=set" in text and "fred=set" in text and "finnhub=unset" in text
    assert keys.summary() == {"alpaca": True, "finnhub": False, "fred": True, "edgar_user_agent": False}


def test_alpaca_needs_both_halves():
    assert not Keys(alpaca_key_id="x").has_alpaca
    assert not Keys(alpaca_secret="y").has_alpaca
    assert Keys(alpaca_key_id="x", alpaca_secret="y").has_alpaca


def test_edgar_placeholder_does_not_count():
    assert not Keys(edgar_user_agent="stock-terminal your.email@example.com").has_edgar
    assert Keys(edgar_user_agent="stock-terminal someone@realmail.net").has_edgar


def test_load_keys_reads_environment(monkeypatch, tmp_path):
    for name in ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "EDGAR_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FRED_API_KEY", "  abc  ")
    keys = load_keys(tmp_path / "no-such.env")
    assert keys.has_fred and keys.fred == "abc"
    assert not keys.has_alpaca and not keys.has_finnhub and not keys.has_edgar


def test_load_keys_reads_env_file(monkeypatch, tmp_path):
    for name in ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "EDGAR_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("ALPACA_PAPER_KEY_ID=id1\nALPACA_PAPER_SECRET_KEY=sec1\n", encoding="utf-8")
    keys = load_keys(tmp_path / ".env")
    assert keys.has_alpaca
