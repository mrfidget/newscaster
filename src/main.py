"""
Main entry point for Newscaster.

Cycle sequence
--------------
1. Fetch RSS articles from all enabled feeds
2. Deduplicate against database (skip already-sent articles)
3. Topic filter + clean  (pipeline.filter_and_clean)
4. Load LLM → generate digest → unload LLM  (pipeline.generate_digest)
5. Load piper → synthesise audio → unload piper  (tts.TTSEngine)
6. Send single Telegram message with audio + digest text
7. Mark all cycle articles as sent in database
8. Sleep until next cycle

Daily housekeeping (audio cache + article history pruning) is handled via
timestamp comparison — no scheduler library required.
"""
import asyncio
import gc
import sys
from datetime import datetime, timedelta, timezone

from .config   import Config
from .logger   import setup_logger
from .database import Database
from .fetcher  import FeedFetcher
from .pipeline import Pipeline
from .tts      import TTSEngine
from .bot      import NewsBot

logger = setup_logger(__name__)


class Newscaster:
    def __init__(self):
        # Load and validate all configuration up front
        self.config = Config.load("/config/feeds.yaml")

        # Reconfigure the root logger now that we have the level from config
        import logging
        logging.getLogger().setLevel(self.config.log_level)

        # ------------------------------------------------------------------
        # Persistent components (stay alive between cycles)
        # ------------------------------------------------------------------
        self.db       = Database(self.config)
        self.fetcher  = FeedFetcher(self.config)
        self.pipeline = Pipeline(self.config)
        self.bot      = NewsBot(self.config)

        # Give the bot references it needs for /status without circular imports
        self.bot.attach(self.db, self.fetcher)

        # Housekeeping state
        self._last_prune: datetime = datetime.now(timezone.utc)

        logger.info("Newscaster initialised")

    # -------------------------------------------------------------------------
    # Main cycle
    # -------------------------------------------------------------------------

    async def fetch_and_send(self):
        """
        One complete fetch→filter→digest→audio→send cycle.
        Every heavyweight resource (LLM, TTS engine) is loaded and unloaded
        within this method so RAM is released before the next sleep.
        """
        logger.info("── Cycle start ──────────────────────────────────────")

        # 1. Fetch
        articles = self.fetcher.fetch_all()

        # 2. Deduplicate
        new_articles = [a for a in articles
                        if not self.db.is_article_sent(a["hash"])]
        logger.info(f"{len(new_articles)} new articles after deduplication")

        if not new_articles:
            logger.info("Nothing new — sleeping until next cycle")
            return

        # 3. Topic filter + clean
        filtered = self.pipeline.filter_and_clean(new_articles)
        if not filtered:
            return

        # 4. LLM digest (load → infer → unload)
        digest = self.pipeline.generate_digest(filtered)
        if not digest:
            logger.error("Digest generation failed — skipping cycle")
            return

        # 5. TTS (load → synthesise → unload)
        audio_file     = None
        audio_duration = 0

        if self.config.tts_enabled:
            tts = TTSEngine(self.config)
            try:
                result = tts.generate(digest)
                if result:
                    audio_file, audio_duration = result
            finally:
                del tts
                gc.collect()
                logger.debug("TTSEngine unloaded")

        # 6. Send
        await self.bot.send_bulletin(
            digest=digest,
            audio_file=audio_file,
            audio_duration=audio_duration,
            article_count=len(filtered),
            articles=filtered,
        )

        # 7. Persist sent-article records
        for article in filtered:
            self.db.mark_as_sent(
                article["hash"],
                article["title"],
                article["url"],
                article["feed_name"],
            )

        logger.info(
            f"── Cycle complete: {len(filtered)} stories, "
            f"audio={'yes' if audio_file else 'no'} ──"
        )

    # -------------------------------------------------------------------------
    # Housekeeping
    # -------------------------------------------------------------------------

    def _run_daily_jobs(self):
        """
        Called at the top of every cycle.  Triggers pruning once per day via
        simple timestamp comparison — no scheduler dependency.
        """
        now = datetime.now(timezone.utc)
        if now - self._last_prune >= timedelta(hours=24):
            result = self.db.prune()
            logger.info(
                f"Daily prune: {result['audio_files']} audio file(s) removed, "
                f"{result['articles']} article record(s) removed"
            )
            self._last_prune = now

    # -------------------------------------------------------------------------
    # Run loop
    # -------------------------------------------------------------------------

    async def run(self):
        """Start the bot, run the first cycle immediately, then loop."""
        await self.bot.start()

        fetch_interval = self.config.fetch_interval_minutes * 60
        logger.info(f"Fetch interval: {self.config.fetch_interval_minutes} minutes")

        # First cycle runs immediately on startup
        await self.fetch_and_send()

        while True:
            try:
                await asyncio.sleep(fetch_interval)
                self._run_daily_jobs()
                await self.fetch_and_send()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Cycle error (will retry next interval): {exc}",
                             exc_info=True)

        logger.info("Shutting down…")
        await self.bot.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    app = Newscaster()
    try:
        await app.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())