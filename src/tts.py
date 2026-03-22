"""
Text-to-Speech using piper-tts (CPU, no PyTorch required).

Usage pattern — load on demand, unload after use:

    tts = TTSEngine(config)
    result = tts.generate(digest_text)
    del tts
    gc.collect()

Changes from the original:
- Accepts Config dataclass instead of a raw dict
- preprocess_for_tts() is applied before synthesis:
    * expands common abbreviations piper reads letter-by-letter
    * rewrites known homographs (LIVE, read, wound) for news context
    * inserts pause punctuation at natural break points
    * strips any leaked markdown formatting
  The rewrite rules are a plain list of (pattern, replacement) pairs —
  add new entries without touching control flow.
"""
import gc
import hashlib
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from pydub import AudioSegment

from .config import Config
from .logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# TTS pre-processing rules
# Each entry is (compiled_regex, replacement_string).
# Applied in order — put more-specific rules before more-general ones.
# ---------------------------------------------------------------------------

def _r(pattern: str, replacement: str, flags: int = re.IGNORECASE):
    return (re.compile(pattern, flags), replacement)


_PREPROCESS_RULES: List[Tuple[re.Pattern, str]] = [
    # --- Strip numbered lists (e.g. '1. ', '2. ') -------------------------
    _r(r"^\d+\.\s+", "", re.MULTILINE),

    # --- Strip markdown formatting ----------------------------------------
    _r(r"\*{1,3}(.*?)\*{1,3}", r"\1"),    # bold / italic
    _r(r"_{1,3}(.*?)_{1,3}",   r"\1"),    # underscore emphasis
    _r(r"#{1,6}\s*",           r""),       # headings
    _r(r"`{1,3}(.*?)`{1,3}",   r"\1"),    # inline code / code block

    # --- Strip numbered/bulleted list formatting --------------------------
    # "1. Some sentence" → "Some sentence"
    _r(r"^\s*\d+\.\s+", r"", re.MULTILINE),
    # "- item" or "* item" → "item"
    _r(r"^\s*[-*]\s+", r"", re.MULTILINE),

    # --- Abbreviation expansion -------------------------------------------
    _r(r"\bU\.S\.\b",    "US"),
    _r(r"\bU\.K\.\b",    "UK"),
    _r(r"\bE\.U\.\b",    "EU"),
    _r(r"\bU\.N\.\b",    "UN"),
    _r(r"\bvs\.\b",      "versus"),
    _r(r"\bapprox\.\b",  "approximately"),
    _r(r"\bDr\.\b",      "Doctor"),
    _r(r"\bMr\.\b",      "Mister"),
    _r(r"\bMrs\.\b",     "Missus"),
    _r(r"\bProf\.\b",    "Professor"),
    _r(r"\bSt\.\b",      "Saint"),
    _r(r"\bGov\.\b",     "Governor"),
    _r(r"\bSen\.\b",     "Senator"),
    _r(r"\bRep\.\b",     "Representative"),
    _r(r"\bGen\.\b",     "General"),
    _r(r"\bLt\.\b",      "Lieutenant"),
    _r(r"\bCol\.\b",     "Colonel"),
    _r(r"\bno\.\b",      "number"),

    # --- Homograph rewrites (news context) --------------------------------
    # "live" as in live broadcast / live fire → happening now
    _r(r"\bLIVE\b",                             "live"),      # caps → adjective
    _r(r"\blive\s+broadcast\b",                 "ongoing broadcast"),
    _r(r"\blive\s+fire\b",                      "active fire"),
    _r(r"\blive\s+blog\b",                      "ongoing coverage"),
    # "read" past tense — avoid /riːd/ mispronunciation
    _r(r"\bhave\s+read\b",                      "have red"),
    _r(r"\bhad\s+read\b",                       "had red"),
    _r(r"\bhas\s+read\b",                       "has red"),
    # "wound" (injury) vs "wound" (past tense of wind)
    # In news context "wound" almost always means injury; leave it as-is —
    # piper handles this reasonably; add an explicit rule only if needed.

    # --- Prosody: insert natural pause punctuation ------------------------
    # Comma after short introductory adverbials before the main clause
    _r(r"\b(Meanwhile|However|Nevertheless|Furthermore|Moreover|"
       r"Additionally|Subsequently|Consequently|In response|"
       r"As a result|At the same time|On the other hand)\s+(?=[A-Z])",
       r"\1, "),
    # Em-dash before "but", "yet", "while", "although" in long sentences
    # (only when not already preceded by a comma or dash)
    _r(r"(?<![,\-–—])\s+(but|yet|while|although)\s+",
       r" — \1 "),
]


def preprocess_for_tts(text: str) -> str:
    """
    Apply all rewrite rules to improve piper's pronunciation and prosody.
    Rules are composable: add new (pattern, replacement) pairs to
    _PREPROCESS_RULES without touching this function.
    """
    for pattern, replacement in _PREPROCESS_RULES:
        text = pattern.sub(replacement, text)
    # Collapse any double spaces introduced by substitutions
    text = re.sub(r"  +", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# TTSEngine
# ---------------------------------------------------------------------------

class TTSEngine:
    def __init__(self, config: Config):
        self.enabled:       bool = config.tts_enabled
        self.cache_enabled: bool = config.audio_cache_enabled
        self.bitrate:       str  = config.tts_bitrate
        self.model_path           = Path(config.tts_model_path)

        self.audio_dir = Path(config.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        if self.enabled:
            self._check_piper()

        logger.info(f"TTSEngine ready (enabled={self.enabled})")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def generate(self, text: str) -> Optional[Tuple[str, int]]:
        """
        Synthesise speech from text.
        Returns (mp3_file_path, duration_seconds) or None on failure.
        Preprocessing is applied before synthesis; cached results bypass synthesis.
        """
        if not self.enabled or not text:
            return None

        processed = preprocess_for_tts(text)
        logger.debug(f"TTS input after preprocessing ({len(processed)} chars)")

        text_hash = self._hash(processed)
        mp3_path  = self._mp3_path(text_hash)

        # Cache hit
        if self.cache_enabled and mp3_path.exists() and mp3_path.stat().st_size > 0:
            try:
                duration = len(AudioSegment.from_file(str(mp3_path))) // 1000
                logger.debug(f"TTS cache hit: {mp3_path.name}")
                return str(mp3_path), duration
            except Exception:
                mp3_path.unlink(missing_ok=True)

        # Synthesise
        try:
            logger.info(f"Synthesising TTS ({len(processed)} chars)")
            wav_path = self._wav_path(text_hash)

            result = subprocess.run(
                ["piper", "--model", str(self.model_path),
                 "--output_file", str(wav_path)],
                input=processed,
                capture_output=True,
                text=True,
                timeout=180,
            )

            if result.returncode != 0:
                logger.error(f"Piper failed: {result.stderr.strip()}")
                return None

            if not wav_path.exists() or wav_path.stat().st_size == 0:
                logger.error("Piper produced no output")
                return None

            mp3_path, duration = self._wav_to_mp3(wav_path, text_hash)
            logger.info(f"TTS done: {mp3_path.name} ({duration}s)")
            return str(mp3_path), duration

        except subprocess.TimeoutExpired:
            logger.error("Piper timed out — digest may be too long")
            return None
        except Exception as exc:
            logger.error(f"TTS generation failed: {exc}")
            return None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _check_piper(self):
        """Verify the piper binary and model file are present at startup."""
        try:
            subprocess.run(["piper", "--help"],
                           capture_output=True, text=True, timeout=5)
            logger.info("Piper binary found")
        except FileNotFoundError:
            logger.error("piper binary not found — install with: pip install piper-tts")
            self.enabled = False
            return
        except Exception as exc:
            logger.error(f"Piper probe failed ({exc}) — TTS disabled")
            self.enabled = False
            return

        if not self.model_path.exists():
            logger.error(
                f"Piper model not found at {self.model_path}. "
                "Run 'make download-voice' and place the .onnx and .onnx.json "
                "files at the configured path. TTS disabled."
            )
            self.enabled = False

    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:16]

    def _wav_path(self, h: str) -> Path:
        return self.audio_dir / f"{h}.wav"

    def _mp3_path(self, h: str) -> Path:
        return self.audio_dir / f"{h}.mp3"

    def _wav_to_mp3(self, wav_path: Path, text_hash: str) -> Tuple[Path, int]:
        mp3_path = self._mp3_path(text_hash)
        try:
            audio    = AudioSegment.from_wav(str(wav_path))
            duration = len(audio) // 1000
            audio.export(str(mp3_path), format="mp3", bitrate=self.bitrate)
            wav_path.unlink(missing_ok=True)
            return mp3_path, duration
        except Exception as exc:
            logger.error(f"WAV→MP3 conversion failed: {exc}")
            return wav_path, 0