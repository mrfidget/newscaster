"""SQLite database — sent-article deduplication, audio cache, and storage pruning."""
import time
from pathlib import Path
from typing import Optional, Tuple
import sqlite3

from .config import Config
from .logger import setup_logger

logger = setup_logger(__name__)


class Database:
    def __init__(self, config: Config):
        self.db_path  = config.db_path
        self.audio_dir = config.audio_dir
        self.audio_retention_days   = config.audio_retention_days
        self.article_retention_days = config.article_retention_days

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_articles (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_hash TEXT UNIQUE NOT NULL,
                    title        TEXT NOT NULL,
                    url          TEXT NOT NULL,
                    feed_name    TEXT NOT NULL,
                    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS audio_cache (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT UNIQUE NOT NULL,
                    audio_file   TEXT NOT NULL,
                    duration     INTEGER,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    access_count INTEGER DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_article_hash
                ON sent_articles(article_hash)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_audio_hash
                ON audio_cache(content_hash)
            """)

            conn.commit()
        logger.info("Database initialised")

    # -------------------------------------------------------------------------
    # Articles
    # -------------------------------------------------------------------------

    def is_article_sent(self, article_hash: str) -> bool:
        """Return True if this article hash has already been recorded."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM sent_articles WHERE article_hash = ?",
                (article_hash,)
            )
            return cursor.fetchone() is not None

    def mark_as_sent(self, article_hash: str, title: str, url: str,
                     feed_name: str) -> int:
        """
        Record that an article was included in a bulletin.
        Returns the new row id, or -1 if the hash already exists.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO sent_articles (article_hash, title, url, feed_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(article_hash) DO NOTHING
                RETURNING id
            """, (article_hash, title, url, feed_name))

            result = cursor.fetchone()
            conn.commit()

            if result:
                logger.debug(f"Marked as sent: {title[:60]}")
                return result[0]
            return -1

    def count_sent_last_24h(self) -> int:
        """Return the number of articles sent in the last 24 hours."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sent_articles "
                "WHERE sent_at >= datetime('now', '-1 day')"
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    # -------------------------------------------------------------------------
    # Audio cache
    # -------------------------------------------------------------------------

    def get_cached_audio(self, content_hash: str) -> Optional[Tuple[str, int]]:
        """Return (file_path, duration_seconds) for a cached digest, or None."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT audio_file, duration FROM audio_cache WHERE content_hash = ?",
                (content_hash,)
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    "UPDATE audio_cache SET access_count = access_count + 1 "
                    "WHERE content_hash = ?",
                    (content_hash,)
                )
                conn.commit()
                return row[0], row[1]
            return None

    def cache_audio(self, content_hash: str, audio_file: str, duration: int):
        """Persist an audio cache entry."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO audio_cache
                    (content_hash, audio_file, duration, access_count)
                VALUES (?, ?, ?, 1)
            """, (content_hash, audio_file, duration))
            conn.commit()
        logger.debug(f"Audio cached: {content_hash}")

    # -------------------------------------------------------------------------
    # Pruning — called once per day from main.py
    # -------------------------------------------------------------------------

    def prune(self) -> dict:
        """
        Daily housekeeping.  Runs two pruning jobs and returns a summary dict:
          - audio files + cache rows older than audio_retention_days
          - sent_articles rows older than article_retention_days

        No OS-level cron required; called from the in-app daily-job hook.
        """
        audio_pruned   = self._prune_audio()
        article_pruned = self._prune_articles()
        return {"audio_files": audio_pruned, "articles": article_pruned}

    def _prune_audio(self) -> int:
        """Delete MP3/WAV files older than audio_retention_days and their DB rows."""
        cutoff  = time.time() - (self.audio_retention_days * 86_400)
        pruned  = 0
        audio_path = Path(self.audio_dir)

        for pattern in ("*.mp3", "*.wav"):
            for f in audio_path.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        pruned += 1
                except Exception as exc:
                    logger.warning(f"Could not prune audio file {f}: {exc}")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM audio_cache WHERE created_at < datetime('now', ?)",
                (f"-{self.audio_retention_days} days",),
            )
            conn.commit()

        if pruned:
            logger.info(
                f"Audio cache pruned: {pruned} file(s) older than "
                f"{self.audio_retention_days}d removed"
            )
        return pruned

    def _prune_articles(self) -> int:
        """Remove sent_articles rows older than article_retention_days."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM sent_articles WHERE sent_at < datetime('now', ?)",
                (f"-{self.article_retention_days} days",),
            )
            conn.commit()
            deleted = cursor.rowcount

        if deleted:
            logger.info(
                f"Article history pruned: {deleted} row(s) older than "
                f"{self.article_retention_days}d removed"
            )
        return deleted