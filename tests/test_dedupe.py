from stock_terminal.news.dedupe import find_original, is_same_story, similarity, story_key, tokens


def test_tokens_drop_stopwords_and_stem():
    assert tokens("Nvidia shares fall after earnings report") == {"nvidia", "fall", "earning"}
    assert story_key("The Fed Holds Rates") == story_key("fed holds rates")


def test_same_wire_story_from_two_outlets(make_item):
    a = make_item("alpaca:1", "Nvidia shares fall after earnings report", hours_ago=2, source="alpaca/benzinga")
    b = make_item("finnhub:9", "Nvidia shares fall after earnings report - Reuters", hours_ago=1, source="finnhub/Reuters")
    assert is_same_story(a, b)
    assert similarity(tokens(a.headline), tokens(b.headline)) > 0.5


def test_same_url_is_same_story(make_item):
    a = make_item("x:1", "One headline", url="https://www.example.com/story?utm=1")
    b = make_item("x:2", "Completely different words", url="https://example.com/story/")
    assert is_same_story(a, b)


def test_normalise_url_keeps_the_query_that_identifies_the_article():
    from stock_terminal.news.dedupe import normalise_url
    assert normalise_url("https://finnhub.io/api/news?id=abc") != normalise_url("https://finnhub.io/api/news?id=def")
    assert normalise_url("https://www.Example.com/story/?utm_source=x&id=7#top") == "example.com/story?id=7"
    assert normalise_url("https://example.com/story?utm=1") == normalise_url("https://example.com/story/")


def test_different_stories_stay_apart(make_item):
    a = make_item("x:1", "Apple unveils new iPhone with thinner body")
    b = make_item("x:2", "Apple faces antitrust lawsuit in the EU")
    assert not is_same_story(a, b)


def test_identical_headline_days_apart_is_not_a_duplicate(make_item):
    a = make_item("x:1", "Fed holds rates steady", hours_ago=100)
    b = make_item("x:2", "Fed holds rates steady", hours_ago=1)
    assert not is_same_story(a, b)


def test_templated_headlines_about_different_companies_are_different_stories(make_item):
    a = make_item("x:1", "Why Apple Stock Rose Today", symbols=["AAPL"])
    b = make_item("x:2", "Why Nvidia Stock Rose Today", symbols=["NVDA"])
    assert not is_same_story(a, b)                                  # no ticker in common: never the same story
    c = make_item("x:3", "Why Apple Stock Fell Today", symbols=["AAPL"])
    assert not is_same_story(a, c)                                  # same company, opposite story: 3 of 5 words is under 70%
    d = make_item("x:4", "Why Apple Stock Rose Today - Reuters", symbols=["AAPL", "MSFT"])
    assert is_same_story(a, d)                                      # shared ticker and 4 of 5 words (80%)
    e = make_item("x:5", "Why Apple Stock Rose Today - Motley Fool", symbols=["AAPL"])
    assert not is_same_story(a, e)                                  # 4 of 6 words (67%) is under the bar on purpose


def test_find_original_prefers_the_earliest_and_skips_duplicates(make_item):
    first = make_item("x:1", "Microsoft raises cloud guidance", hours_ago=10)
    second = make_item("x:2", "Microsoft raises cloud guidance again", hours_ago=5)
    second.duplicate_of = "x:1"
    new = make_item("x:3", "Microsoft raises its cloud guidance", hours_ago=1)
    assert find_original(new, [second, first]) is first
    assert find_original(new, []) is None
