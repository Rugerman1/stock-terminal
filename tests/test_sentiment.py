from stock_terminal.news.sentiment import FinBertScorer, NoScorer, build_scorer, finbert_score


def test_finbert_score_is_positive_minus_negative():
    labels = [{"label": "positive", "score": 0.7}, {"label": "negative", "score": 0.1}, {"label": "neutral", "score": 0.2}]
    assert abs(finbert_score(labels) - 0.6) < 1e-9
    assert finbert_score([{"label": "neutral", "score": 1.0}]) == 0.0


class FakePipeline:
    def __init__(self):
        self.calls = []

    def __call__(self, texts, batch_size=16):
        self.calls.append(list(texts))
        out = []
        for text in texts:
            if "beats" in text:
                out.append([{"label": "positive", "score": 0.9}, {"label": "negative", "score": 0.05}, {"label": "neutral", "score": 0.05}])
            else:
                out.append([{"label": "positive", "score": 0.1}, {"label": "negative", "score": 0.6}, {"label": "neutral", "score": 0.3}])
        return out


def test_scorer_batches_and_scores():
    pipe = FakePipeline()
    scorer = FinBertScorer(batch_size=2, pipeline_factory=lambda: pipe)
    assert scorer.status().startswith("idle")
    scores = scorer.score(["Apple beats estimates", "Apple misses", "Apple beats again"])
    assert [round(s, 2) for s in scores] == [0.85, -0.5, 0.85]
    assert len(pipe.calls) == 2 and scorer.status().startswith("ok")
    assert scorer.score([]) == []


def test_load_failure_means_missing_not_crash():
    def boom():
        raise OSError("no model files")

    scorer = FinBertScorer(pipeline_factory=boom)
    assert scorer.score(["x", "y"]) == [None, None]
    assert scorer.status().startswith("MISSING: OSError")
    assert scorer.score(["z"]) == [None]   # does not retry the load every call


def test_no_scorer_and_config_switch(cfg):
    none = NoScorer("sentiment disabled in config.yaml")
    assert none.score(["a"]) == [None] and "disabled" in none.status()
    cfg["sentiment"]["enabled"] = False
    assert isinstance(build_scorer(cfg), NoScorer)
    cfg["sentiment"]["enabled"] = True
    assert isinstance(build_scorer(cfg), FinBertScorer)   # transformers and torch are installed in the venv
