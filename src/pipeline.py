"""
pipeline.py — topic filtering, article cleaning, and LLM digest generation.

Changes from the original:
- Accepts Config dataclass instead of a raw dict
- max_tokens is now auto-scaled per batch: min(80 + n * 25, llm_max_tokens)
  where llm_max_tokens acts as a ceiling (default 400).  This prevents the
  model hitting its token limit mid-sentence on larger batches while avoiding
  over-allocation on small ones.
"""
import gc
import re
from datetime import datetime as _dt
from typing import Any, Dict, List, Optional

from .config import Config
from .logger import setup_logger

logger = setup_logger(__name__)

_EPOCH = _dt(1970, 1, 1)

# Closing phrase appended by Python, never passed to the model.
# Small instruction-tuned models (sub-2B) short-circuit to outputting
# the closing phrase literally when they see "End with: X" in the prompt.
_CLOSING = "That's your briefing."


class Pipeline:
    def __init__(self, config: Config):
        self.config = config
        self.topics: List[str]  = config.topics
        self.max_articles: int  = config.max_articles
        self.min_articles: int  = config.min_articles

        self.model_path: str    = config.llm_model_path
        self.max_tokens_cap: int = config.llm_max_tokens   # ceiling, not fixed
        self.temperature: float = config.llm_temperature
        self.system_prompt: str = config.llm_system_prompt

        self.max_content_chars: int = 200

        logger.info(
            f"Pipeline initialised — {len(self.topics)} topic(s), "
            f"min={self.min_articles}, max={self.max_articles}, "
            f"max_tokens_cap={self.max_tokens_cap}"
        )

    # -------------------------------------------------------------------------
    # Stage 1 — filter and clean
    # -------------------------------------------------------------------------

    def filter_and_clean(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.topics:
            logger.warning("No topics configured — passing all articles through")
            matched = list(articles)
        else:
            matched = [a for a in articles if self._matches_topics(a)]

        logger.info(f"Topic filter: {len(matched)}/{len(articles)} articles matched")

        if len(matched) < self.min_articles:
            logger.warning(
                f"Only {len(matched)} article(s) matched topics "
                f"(minimum is {self.min_articles}) — skipping cycle"
            )
            return []

        matched.sort(key=lambda a: a.get("published") or _EPOCH, reverse=True)

        if len(matched) > self.max_articles:
            logger.info(f"Capping at {self.max_articles} articles ({len(matched)} matched)")
            matched = matched[: self.max_articles]

        for article in matched:
            article["title"]   = self._clean(article.get("title", ""))
            article["content"] = self._clean_and_truncate(
                article.get("content") or article.get("rss_summary", "")
            )

        return matched

    def _matches_topics(self, article: Dict[str, Any]) -> bool:
        haystack = (
            (article.get("title", "") + " " + article.get("content", "")).lower()
        )
        return any(topic in haystack for topic in self.topics)

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"<[^>]+>",       " ", text)
        text = re.sub(r"https?://\S+",  "",  text)
        text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
        text = re.sub(r"\s+",           " ", text)
        return text.strip()

    def _clean_and_truncate(self, text: str) -> str:
        cleaned = self._clean(text)
        if len(cleaned) <= self.max_content_chars:
            return cleaned
        truncated = cleaned[: self.max_content_chars]
        for sep in (". ", "! ", "? "):
            idx = truncated.rfind(sep)
            if idx > self.max_content_chars // 2:
                return truncated[: idx + 1]
        return truncated.rsplit(" ", 1)[0]

    # -------------------------------------------------------------------------
    # Stage 2 — LLM digest generation
    # -------------------------------------------------------------------------

    def _scaled_max_tokens(self, n_articles: int) -> int:
        """
        Auto-scale the token budget to the batch size.
        Formula: min(80 + n * 25, cap)
        - 3 articles → 155 tokens
        - 5 articles → 205 tokens
        - 10 articles → 330 tokens
        The cap (llm_max_tokens from config) is the hard ceiling.
        """
        scaled = 80 + n_articles * 25
        return min(scaled, self.max_tokens_cap)

    @staticmethod
    def _chatml(system: str, user: str) -> str:
        """Render a chatml prompt ending with the open assistant turn."""
        return (
            f"<|im_start|>system\n{system}\n<|im_end|>\n"
            f"<|im_start|>user\n{user}\n<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

    def generate_digest(self, articles: List[Dict[str, Any]]) -> Optional[str]:
        """
        Load the GGUF model, generate a bulletin digest, unload immediately.
        Returns the digest string (with closing phrase appended), or None on failure.
        """
        if not articles:
            return None

        max_tokens = self._scaled_max_tokens(len(articles))
        stories_block = self._build_prompt(articles)
        user_content = (
            f"Write a radio news bulletin covering the {len(articles)} stories below.\n"
            "Rules:\n"
            "- Write in flowing prose only — sentence after sentence, separated by a full stop and a space.\n"
            "- Each sentence must be based only on the provided summary.\n"
            "- Do NOT number the sentences. Do NOT use bullet points, bold, or markdown.\n"
            "- Do NOT write headlines or titles — write complete sentences only.\n"
            "- Total length: under 100 words.\n"
            "- Do not add a sign-off or closing line.\n\n"
            "Example of correct output format:\n"
            "Flooding in southern Spain has displaced thousands as rescue teams work through the night. "
            "The European Central Bank held interest rates steady citing persistent inflation. "
            "A ceasefire in Sudan has collapsed after less than 48 hours.\n\n"
            "Now write the bulletin for these stories:\n\n"
            + stories_block
        )

        prompt = self._chatml(self.system_prompt.strip(), user_content)

        logger.info(
            f"Generating digest for {len(articles)} article(s) via "
            f"{self.model_path} (max_tokens={max_tokens})"
        )
        logger.debug(f"Rendered prompt:\n{prompt}")

        llm = None
        try:
            from llama_cpp import Llama

            llm = Llama(
                model_path=self.model_path,
                n_ctx=4096,
                n_threads=4,
                verbose=False,
            )

            response = llm.create_completion(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=self.temperature,
                stop=[],
                echo=False,
            )

            raw = response["choices"][0]["text"]
            raw = raw.split("<|im_end|>")[0].split("<|endoftext|>")[0]
            raw = raw.strip()

            logger.info(f"Raw LLM response: {raw!r}")

            if not raw:
                logger.error("LLM returned empty response — skipping cycle")
                return None

            word_count = len(raw.split())
            if word_count < 5:
                logger.warning(
                    f"Digest suspiciously short ({word_count} words): {raw!r} — "
                    "the model may not be following instructions."
                )

            digest = f"{raw}\n{_CLOSING}"
            logger.info(f"Digest ready ({word_count} words + closing phrase)")
            return digest

        except FileNotFoundError:
            logger.error(
                f"GGUF model not found at {self.model_path}. "
                "Run 'make download-llm' to fetch it."
            )
            return None
        except Exception as exc:
            logger.error(f"LLM digest generation failed: {exc}", exc_info=True)
            return None
        finally:
            if llm is not None:
                del llm
            gc.collect()
            logger.debug("LLM unloaded and GC collected")

    @staticmethod
    def _build_prompt(articles: List[Dict[str, Any]]) -> str:
        lines = []
        for i, article in enumerate(articles, start=1):
            title   = article.get("title", "Untitled")
            summary = article.get("content", "").strip() or title
            lines.append(f"{i}. TITLE: {title}\n   SUMMARY: {summary}")
        return "\n\n".join(lines)