"""
config.py — typed configuration dataclass, single source of truth.

Loaded once in main.py and passed to every component.
No other module should read os.environ or yaml directly.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

from .logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class FeedConfig:
    name: str
    url: str
    enabled: bool = True


@dataclass
class Config:
    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    telegram_bot_token: str
    telegram_chat_id: int

    # ------------------------------------------------------------------
    # Feeds
    # ------------------------------------------------------------------
    feeds: List[FeedConfig]
    fetch_interval_minutes: int = 30

    # ------------------------------------------------------------------
    # Article filtering
    # ------------------------------------------------------------------
    topics: List[str] = field(default_factory=list)
    min_articles: int = 3
    max_articles: int = 10

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    llm_model_path: str = "/models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    llm_max_tokens: int = 300
    llm_temperature: float = 0.3
    llm_system_prompt: str = (
        "You are a concise radio news bulletin writer. "
        "You write short, factual summaries of news stories."
    )

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------
    tts_enabled: bool = True
    tts_model_path: str = "/models/tts/en_GB-jenny_dioco-medium.onnx"
    tts_bitrate: str = "64k"
    audio_cache_enabled: bool = True

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    db_path: str = "/data/newscaster.db"
    audio_dir: str = "/data/audio"
    audio_retention_days: int = 30
    article_retention_days: int = 7

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, feeds_yaml_path: str = "/config/feeds.yaml") -> "Config":
        """
        Build a Config instance from environment variables and feeds.yaml.
        Exits with a clear message if required values are missing.
        """
        load_dotenv()

        # --- Required env vars -------------------------------------------
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "")

        if not token or token == "your_bot_token_here":
            logger.error("TELEGRAM_BOT_TOKEN is not set in the environment / .env")
            sys.exit(1)

        if not chat_id_raw or chat_id_raw == "your_chat_id_here":
            logger.error("TELEGRAM_CHAT_ID is not set in the environment / .env")
            sys.exit(1)

        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            logger.error("TELEGRAM_CHAT_ID must be an integer")
            sys.exit(1)

        # --- feeds.yaml --------------------------------------------------
        yaml_path = Path(feeds_yaml_path)
        if not yaml_path.exists():
            logger.error(f"Config file not found: {yaml_path}")
            sys.exit(1)

        with open(yaml_path) as fh:
            raw: Dict[str, Any] = yaml.safe_load(fh) or {}

        feeds = [
            FeedConfig(
                name=f["name"],
                url=f["url"],
                enabled=f.get("enabled", True),
            )
            for f in raw.get("feeds", [])
        ]

        llm_cfg  = raw.get("llm", {})
        tts_cfg  = raw.get("tts", {})
        summ_cfg = raw.get("summarization", {})

        return cls(
            # Telegram
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            # Feeds
            feeds=feeds,
            fetch_interval_minutes=int(raw.get("fetch_interval", 30)),
            # Filtering
            topics=[t.lower() for t in raw.get("topics", [])],
            min_articles=int(raw.get("min_articles_per_cycle", 3)),
            max_articles=int(raw.get("max_articles_per_cycle", 10)),
            # LLM
            llm_model_path=llm_cfg.get(
                "model_path", "/models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf"
            ),
            llm_max_tokens=int(llm_cfg.get("max_tokens", 300)),
            llm_temperature=float(llm_cfg.get("temperature", 0.3)),
            llm_system_prompt=llm_cfg.get(
                "system_prompt",
                "You are a concise radio news bulletin writer. "
                "You write short, factual summaries of news stories.",
            ),
            # TTS
            tts_enabled=tts_cfg.get("enabled", True),
            tts_model_path=tts_cfg.get(
                "model_path", "/models/tts/en_GB-jenny_dioco-medium.onnx"
            ),
            tts_bitrate=tts_cfg.get("bitrate", "64k"),
            audio_cache_enabled=tts_cfg.get("cache_audio", True),
            # Storage
            db_path="/data/newscaster.db",
            audio_dir="/data/audio",
            audio_retention_days=int(
                raw.get("audio_cache_max_age_days", 30)
            ),
            article_retention_days=int(
                raw.get("article_retention_days", 7)
            ),
            # Logging
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )