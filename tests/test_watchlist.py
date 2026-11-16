from stock_terminal.watchlist import HEADER, Watchlist, is_valid, normalise


def test_normalise_and_validate():
    assert normalise("  tsla ") == "TSLA"
    assert is_valid("BRK.B") and is_valid("BF-B") and is_valid("A")
    assert not is_valid("") and not is_valid("123") and not is_valid("TOO-LONG-TICKER") and not is_valid("A B")


def test_load_normalises_dedupes_and_skips_junk(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text("tickers:\n- aapl\n- MSFT\n- 'bad ticker!'\n- aapl\n- 123\n", encoding="utf-8")
    wl = Watchlist(path)
    assert wl.tickers == ["AAPL", "MSFT"]


def test_missing_file_starts_empty(tmp_path):
    wl = Watchlist(tmp_path / "none.yaml")
    assert wl.tickers == [] and len(wl) == 0


def test_add_saves_and_reloads(tmp_path):
    path = tmp_path / "w.yaml"
    wl = Watchlist(path)
    assert wl.add(" tsla ") == "TSLA"
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEADER) and "- TSLA" in text
    assert Watchlist(path).tickers == ["TSLA"]
    assert "tsla" in wl


def test_add_rejects_duplicate_and_invalid(tmp_path):
    wl = Watchlist(tmp_path / "w.yaml")
    assert wl.add("TSLA") == "TSLA"
    assert wl.add("tsla") is None
    assert wl.add("") is None
    assert wl.add("$$") is None
    assert wl.tickers == ["TSLA"]


def test_remove(tmp_path):
    path = tmp_path / "w.yaml"
    wl = Watchlist(path)
    wl.add("AAPL")
    wl.add("MSFT")
    assert wl.remove("aapl") is True
    assert wl.remove("AAPL") is False
    assert Watchlist(path).tickers == ["MSFT"]
