#!/usr/bin/env python3
"""
scripts/benchmark_llm.py

Compare one or more GGUF models on a fixed set of sample articles.

Usage:
    python scripts/benchmark_llm.py --model /path/to/model.gguf
    python scripts/benchmark_llm.py --model /path/to/model.gguf --articles sample.json

Invoked automatically via:
    make test-llm

The script measures wall-clock inference time and prints the digest output
alongside timing, so you can make an informed choice between e.g.
Qwen2.5-0.5B (~400 MB, faster) and Qwen2.5-1.5B (~1 GB, higher quality).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Allow running both as a script (python scripts/benchmark_llm.py) and via
# docker compose run which sets WORKDIR=/app and has src/ on the path.
# ---------------------------------------------------------------------------
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# ---------------------------------------------------------------------------
# Sample articles — used when no --articles file is supplied.
# Cover the default topic areas: war, AI, climate, economy, Iran, politics,
# trade.  Realistic enough to exercise the model's news-bulletin capabilities.
# ---------------------------------------------------------------------------
_SAMPLE_ARTICLES: List[Dict[str, Any]] = [
    {
        "hash": "bench001",
        "title": "Ukraine frontline offensive stalls amid heavy losses",
        "url": "https://example.com/1",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "Ukrainian forces have suspended a counteroffensive in the eastern "
            "Zaporizhzhia region after suffering significant casualties over the "
            "past 48 hours. Commanders cited exhausted supply lines and intensified "
            "Russian drone attacks as primary factors."
        ),
    },
    {
        "hash": "bench002",
        "title": "OpenAI releases new reasoning model with improved coding benchmarks",
        "url": "https://example.com/2",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "OpenAI has announced a new large language model claiming state-of-the-art "
            "performance on software engineering and mathematics benchmarks. The company "
            "says the model uses an extended chain-of-thought process and is available "
            "via API starting today."
        ),
    },
    {
        "hash": "bench003",
        "title": "Arctic sea ice reaches record low for March",
        "url": "https://example.com/3",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "Satellite data from the National Snow and Ice Data Center shows Arctic "
            "sea-ice extent has fallen to its lowest recorded level for this time of "
            "year, accelerating concerns among climate scientists about a feedback loop "
            "that could further warm the region."
        ),
    },
    {
        "hash": "bench004",
        "title": "IMF warns of slowing global growth as trade barriers rise",
        "url": "https://example.com/4",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "The International Monetary Fund has cut its global growth forecast to 2.8% "
            "for the coming year, citing the cumulative drag of new tariffs, tightening "
            "credit conditions, and weak consumer demand in advanced economies."
        ),
    },
    {
        "hash": "bench005",
        "title": "Iran nuclear talks resume in Vienna with new European proposal",
        "url": "https://example.com/5",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "Diplomats from the E3 — France, Germany, and the United Kingdom — "
            "have submitted a revised framework to Iranian negotiators in Vienna aimed "
            "at limiting uranium enrichment in exchange for partial sanctions relief. "
            "Tehran has not yet publicly responded."
        ),
    },
    {
        "hash": "bench006",
        "title": "US Senate passes sweeping AI liability legislation",
        "url": "https://example.com/6",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "The US Senate has passed a bill that would hold AI developers legally "
            "liable for harms caused by their systems in high-risk sectors including "
            "healthcare, finance, and critical infrastructure. The legislation now "
            "moves to the House of Representatives."
        ),
    },
    {
        "hash": "bench007",
        "title": "EU imposes new tariffs on Chinese electric vehicles",
        "url": "https://example.com/7",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "The European Commission has confirmed additional duties of up to 35% on "
            "electric vehicles imported from China, following an anti-subsidy "
            "investigation. Beijing has threatened retaliatory measures targeting "
            "European agricultural exports."
        ),
    },
    {
        "hash": "bench008",
        "title": "Brazilian election results trigger political uncertainty",
        "url": "https://example.com/8",
        "feed_name": "Sample",
        "published": None,
        "rss_summary": "",
        "content": (
            "Preliminary results from Brazil's regional elections show a fragmented "
            "outcome with no single coalition gaining a clear majority. Analysts warn "
            "the result could hamper the government's ability to pass fiscal reform "
            "legislation before the end of the year."
        ),
    },
]


# ---------------------------------------------------------------------------
# Minimal stub Config so we can instantiate Pipeline without a live .env
# ---------------------------------------------------------------------------

def _make_stub_config(model_path: str):
    """Return a minimal object that satisfies Pipeline's attribute access."""
    class _Cfg:
        topics              = ["war", "artificial intelligence", "climate",
                               "economy", "iran", "politics", "trade"]
        min_articles        = 1
        max_articles        = 10
        llm_model_path      = model_path
        llm_max_tokens      = 400
        llm_temperature     = 0.3
        llm_system_prompt   = (
            "You are a concise radio news bulletin writer. "
            "You write short, factual summaries of news stories."
        )
    return _Cfg()


def _load_articles(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return _SAMPLE_ARTICLES
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            print(f"ERROR: {path} must contain a JSON array of article objects.")
            sys.exit(1)
        return data
    except Exception as exc:
        print(f"ERROR loading articles from {path}: {exc}")
        sys.exit(1)


def _separator(char: str = "─", width: int = 64) -> str:
    return char * width


def run_benchmark(model_path: str, articles: List[Dict[str, Any]]) -> None:
    model_file = Path(model_path)
    if not model_file.exists():
        print(f"  SKIP  {model_path}  (file not found)")
        return

    print(_separator("═"))
    print(f"  Model : {model_file.name}")
    print(f"  Size  : {model_file.stat().st_size / 1_048_576:.1f} MB")
    print(f"  Inputs: {len(articles)} article(s)")
    print(_separator())

    # Import here so a missing llama-cpp-python gives a clear error
    try:
        from src.pipeline import Pipeline
    except ImportError as exc:
        print(f"  ERROR: could not import Pipeline — {exc}")
        return

    cfg      = _make_stub_config(model_path)
    pipeline = Pipeline(cfg)

    t_start  = time.perf_counter()
    digest   = pipeline.generate_digest(articles)
    elapsed  = time.perf_counter() - t_start

    print()
    if digest:
        print("  Digest output:")
        print(_separator("·"))
        for line in digest.splitlines():
            print(f"    {line}")
        print(_separator("·"))
        word_count = len(digest.split())
        print(f"  Words  : {word_count}")
    else:
        print("  ERROR: generate_digest() returned None")

    print(f"  Time   : {elapsed:.2f}s")
    print(_separator("═"))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark one GGUF model against the Newscaster pipeline."
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Path to the GGUF model file to benchmark",
    )
    parser.add_argument(
        "--articles",
        default=None,
        help="Optional path to a JSON file containing sample articles "
             "(array of objects with title/content/feed_name keys). "
             "Defaults to a built-in set of 8 realistic news articles.",
    )
    args = parser.parse_args()

    articles = _load_articles(args.articles)
    run_benchmark(args.model, articles)


if __name__ == "__main__":
    main()