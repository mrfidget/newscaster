"""
Telegram bot — sends a single bulletin per cycle (audio + text digest).

Changes:
- Accepts Config dataclass
- /status command: last cycle time, 24 h article count, feed health,
  model filenames, uptime
- Bulletin caption reformatted: each digest sentence on its own line
- Article links included in messages ("Read more" section)
- Caption budget management: Telegram limits audio captions to 1024 chars
  and text messages to 4096 chars. Links are dropped first if over budget,
  then story bullets are trimmed from the bottom. Text messages (no audio)
  get the full 4096 char budget and always include links.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import Config
from .logger import setup_logger

if TYPE_CHECKING:
    from .database import Database
    from .fetcher  import FeedFetcher

logger = setup_logger(__name__)

# Telegram hard limits
_AUDIO_CAPTION_LIMIT = 1024
_TEXT_MESSAGE_LIMIT  = 4096


class NewsBot:
    def __init__(self, config: Config):
        self.token   = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.config  = config
        self.app: Optional[Application] = None

        self._started_at: datetime = datetime.now(timezone.utc)
        self._last_cycle_at: Optional[datetime] = None
        self._last_cycle_count: int = 0

        self._db: Optional["Database"]         = None
        self._fetcher: Optional["FeedFetcher"] = None

    def attach(self, db: "Database", fetcher: "FeedFetcher"):
        self._db      = db
        self._fetcher = fetcher

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start",  self._start_command))
        self.app.add_handler(CommandHandler("help",   self._help_command))
        self.app.add_handler(CommandHandler("status", self._status_command))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Bot started and polling")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("Bot stopped")

    # -------------------------------------------------------------------------
    # Sending
    # -------------------------------------------------------------------------

    async def send_bulletin(
        self,
        digest: str,
        audio_file: Optional[str] = None,
        audio_duration: int = 0,
        article_count: int = 0,
        articles: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        Send one bulletin message per cycle.

        articles: filtered article list from the pipeline, used to build
                  the 'Read more' links section.
        Audio captions are limited to 1024 chars; text messages to 4096.
        Links are dropped first when over budget, then bullets trimmed.
        """
        if not self.app:
            logger.error("Bot not started — cannot send bulletin")
            return

        self._last_cycle_at    = datetime.now(timezone.utc)
        self._last_cycle_count = article_count
        articles = articles or []

        header = (
            f"📻 *Newscaster Bulletin* "
            f"— {article_count} stor{'y' if article_count == 1 else 'ies'}"
        )

        try:
            if audio_file:
                # Audio caption: digest only, no links (1024 char limit too tight)
                caption = self._build_caption(
                    header, digest, [], limit=_AUDIO_CAPTION_LIMIT
                )
                with open(audio_file, "rb") as audio:
                    await self.app.bot.send_audio(
                        chat_id=self.chat_id,
                        audio=audio,
                        title="Newscaster Bulletin",
                        performer="Newscaster",
                        duration=audio_duration,
                        caption=caption,
                        parse_mode="Markdown",
                    )
                logger.info(
                    f"Sent audio bulletin ({audio_duration}s, "
                    f"{article_count} stories, {len(caption)} caption chars)"
                )
                # Send links as a separate follow-up message
                links_block = self._build_links_block(articles)
                if links_block:
                    await self.app.bot.send_message(
                        chat_id=self.chat_id,
                        text=links_block,
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                    logger.info(f"Sent links follow-up ({len(links_block)} chars)")
            else:
                # Text message: digest + links together (4096 char limit is ample)
                text = self._build_caption(
                    header, digest, articles, limit=_TEXT_MESSAGE_LIMIT
                )
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                logger.info(
                    f"Sent text bulletin ({article_count} stories, {len(text)} chars)"
                )

        except Exception as exc:
            logger.error(f"Failed to send bulletin: {exc}")

    # -------------------------------------------------------------------------
    # Caption building
    # -------------------------------------------------------------------------

    def _build_caption(
        self,
        header: str,
        digest: str,
        articles: List[Dict[str, Any]],
        limit: int,
    ) -> str:
        """
        Assemble and trim the caption to fit within the Telegram character limit.

        Trimming order:
          1. Drop the links section entirely
          2. Trim story bullets from the bottom with '… and N more'
        The header and closing phrase are never dropped.
        """
        links_block  = self._build_links_block(articles)
        digest_block = self._format_digest(digest)

        # Try full version
        full = self._assemble(header, digest_block, links_block)
        if len(full) <= limit:
            return full

        # Drop links
        no_links = self._assemble(header, digest_block, "")
        if len(no_links) <= limit:
            logger.debug("Caption over limit — dropped links section")
            return no_links

        # Trim bullets
        logger.debug(f"Caption trimmed to fit {limit} char limit")
        return self._trim_digest_to_fit(header, digest_block, limit)

    @staticmethod
    def _assemble(header: str, digest_block: str, links_block: str) -> str:
        parts = [header, "", digest_block]
        if links_block:
            parts += ["", links_block]
        return "\n".join(parts)

    @staticmethod
    def _build_links_block(articles: List[Dict[str, Any]]) -> str:
        """Build a 'Read more' section with one clickable link per article."""
        if not articles:
            return ""
        lines = ["📎 *Read more:*"]
        for article in articles:
            title = article.get("title", "Article")
            url   = article.get("url", "")
            if url:
                # Only escape square brackets — they break the link syntax.
                # Over-escaping other chars causes Telegram to reject the message.
                safe_title = title.replace("[", r"\[").replace("]", r"\]")
                lines.append(f"• [{safe_title}]({url})")
        result = "\n".join(lines) if len(lines) > 1 else ""
        logger.debug(f"Links block: {len(lines)-1} links, {len(result)} chars")
        return result

    def _trim_digest_to_fit(
        self, header: str, digest_block: str, limit: int
    ) -> str:
        """Remove bullets from the bottom until the caption fits."""
        closing_markers = ("that's your briefing", "that is your briefing")
        lines = digest_block.split("\n")

        closing_line = ""
        if lines and lines[-1].strip().lower().rstrip(".") in closing_markers:
            closing_line = lines[-1]
            bullet_lines = lines[:-2]
        else:
            bullet_lines = lines

        bullets = [l for l in bullet_lines if l.startswith("▸")]
        dropped = 0

        while bullets:
            suffix  = f"\n… and {dropped} more" if dropped else ""
            closing = f"\n\n{closing_line}" if closing_line else ""
            rebuilt = "\n".join(bullets) + suffix + closing
            if len(self._assemble(header, rebuilt, "")) <= limit:
                return self._assemble(header, rebuilt, "")
            bullets.pop()
            dropped += 1

        return f"{header}\n\n{closing_line or '_No digest available._'}"

    # -------------------------------------------------------------------------
    # Digest formatting
    # -------------------------------------------------------------------------

    @staticmethod
    def _format_digest(digest: str) -> str:
        """One sentence per line with ▸ bullets; closing phrase italicised."""
        # Split only on sentence-ending punctuation followed by a space and
        # a capital letter — avoids splitting on abbreviations like A.K.M.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\'\u2018\u2019])", digest) if s.strip()]
        # If no splits found (e.g. single sentence), fall back to the whole digest
        if not sentences:
            sentences = [digest.strip()]
        if not sentences:
            return digest

        closing_markers = ("that's your briefing", "that is your briefing")
        if sentences[-1].lower() in closing_markers or \
                sentences[-1].lower().rstrip(".") in closing_markers:
            body    = sentences[:-1]
            closing = sentences[-1]
        else:
            body    = sentences
            closing = None

        formatted = "\n".join(f"▸ {s}" for s in body)
        if closing:
            formatted += f"\n\n_{closing}_"
        return formatted

    # -------------------------------------------------------------------------
    # Command handlers
    # -------------------------------------------------------------------------

    async def _start_command(self, update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📻 *Newscaster* is running.\n\n"
            "A fresh bulletin will arrive automatically every cycle.\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    async def _help_command(self, update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Commands:\n"
            "/start  — Show status\n"
            "/help   — Show this message\n"
            "/status — System health and last cycle info\n\n"
            "Bulletins are delivered automatically on each fetch cycle.",
            parse_mode="Markdown",
        )

    async def _status_command(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
        now    = datetime.now(timezone.utc)
        uptime = now - self._started_at

        uptime_hours   = int(uptime.total_seconds() // 3600)
        uptime_minutes = int((uptime.total_seconds() % 3600) // 60)
        uptime_str     = f"{uptime_hours}h {uptime_minutes}m"

        if self._last_cycle_at:
            delta    = now - self._last_cycle_at
            mins_ago = int(delta.total_seconds() // 60)
            cycle_str = (
                f"{mins_ago}m ago "
                f"({self._last_cycle_count} stor{'y' if self._last_cycle_count == 1 else 'ies'})"
            )
        else:
            cycle_str = "no cycle completed yet"

        sent_24h  = self._db.count_sent_last_24h() if self._db else "n/a"

        if self._fetcher:
            feed_lines = []
            for f in self._fetcher.feed_health():
                icon = "✅" if f["status"] == "ok" else (
                       "⛔" if "suspended" in f["status"] else "⚠️")
                feed_lines.append(f"  {icon} {f['name']}: {f['status']}")
            feeds_str = "\n".join(feed_lines) if feed_lines else "  (no feeds)"
        else:
            feeds_str = "  (unavailable)"

        llm_name = Path(self.config.llm_model_path).name
        tts_name = Path(self.config.tts_model_path).name if self.config.tts_enabled else "disabled"

        text = (
            "📡 *Newscaster Status*\n\n"
            f"⏱ Uptime: `{uptime_str}`\n"
            f"🔄 Last cycle: {cycle_str}\n"
            f"📰 Articles sent (24 h): {sent_24h}\n"
            f"⏰ Fetch interval: every {self.config.fetch_interval_minutes} min\n\n"
            f"*Feeds:*\n{feeds_str}\n\n"
            f"*Models:*\n"
            f"  🧠 LLM: `{llm_name}`\n"
            f"  🔊 TTS: `{tts_name}`"
        )

        await update.message.reply_text(text, parse_mode="Markdown")