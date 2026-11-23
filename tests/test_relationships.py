from stock_terminal.relationships import Relationships, catalyst_label, compile_keywords, peers_snippet


def test_shipped_map_loads(rmap):
    aapl = rmap.for_ticker("aapl")
    assert aapl.listed and aapl.sector_etf == "XLK" and len(aapl.peers) == 5
    assert "rates" in aapl.topics and "Apple" in aapl.aliases
    assert aapl.symbols[0] == "AAPL" and "XLK" in aapl.symbols and "MSFT" in aapl.symbols
    assert rmap.sector_name("SMH") == "Semiconductors"


def test_unknown_ticker_gets_an_empty_map(rmap):
    tsla = rmap.for_ticker("TSLA")
    assert not tsla.listed and tsla.peers == [] and tsla.sector_etf is None and tsla.symbols == ["TSLA"]
    assert rmap.aliases_for("TSLA") == ["TSLA"]


def test_catalyst_keywords_and_precedence(rmap):
    assert rmap.catalyst_of("Apple reports record quarterly earnings") == "earnings"
    assert rmap.catalyst_of("Analyst upgrades Nvidia on AI demand") == "analyst"
    assert rmap.catalyst_of("Microsoft agrees to buy a cloud startup") == "mna"
    assert rmap.catalyst_of("Fed holds interest rates steady") == "macro"
    assert rmap.catalyst_of("A quiet day on Wall Street") == "other"
    # Two matches: the first type in the table wins (earnings is listed before analyst).
    assert rmap.catalyst_of("Nvidia earnings beat; analyst raises price target") == "earnings"
    assert catalyst_label("mna") == "M&A" and catalyst_label("legal") == "legal/regulatory" and catalyst_label("zzz") == "zzz"


def test_topics_and_sectors(rmap):
    assert rmap.topics_in("Fed signals a rate cut as inflation cools") == ["rates", "inflation"]
    assert rmap.topics_in("Nothing macro here") == []
    assert rmap.sectors_in("Chip stocks rally after export control news") == ["SMH"]


def test_mentions_match_whole_words_only(rmap):
    assert rmap.mentions("Apple unveils a thinner iPhone", "AAPL")
    assert rmap.mentions("AAPL slips 2%", "AAPL")
    assert not rmap.mentions("Pineapple farms report a bumper crop", "AAPL")
    assert rmap.mentions("Nvidia CEO Jensen Huang speaks", "NVDA")


def test_keyword_regex_is_case_insensitive_and_whole_word():
    pattern = compile_keywords(["fed", "rate cut"])
    assert pattern.search("The FED is done") and pattern.search("a Rate Cut looms")
    assert not pattern.search("federal budget") and not pattern.search("rated cuts")


def test_missing_file_gives_empty_map(tmp_path):
    rmap = Relationships.load(tmp_path / "nope.yaml")
    assert rmap.for_ticker("AAPL").listed is False
    assert rmap.catalyst_of("Apple reports earnings") == "other"
    assert rmap.topics_in("Fed hikes rates") == []


def test_peers_snippet_drops_self_and_caps_at_five():
    text = peers_snippet("AAPL", ["AAPL", "MSFT", "GOOGL", "DELL", "HPQ", "SONY", "WDC"], "XLK")
    assert "  AAPL:" in text and "peers: [MSFT, GOOGL, DELL, HPQ, SONY]" in text and "sector_etf: XLK" in text
