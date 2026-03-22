# Newscaster

A self-hosted, CPU-only news radio bot that runs entirely on your own hardware. Every cycle it fetches RSS articles, filters them by topic, writes a bulletin using a local LLM, synthesises it to speech using a local TTS engine, and delivers it to a Telegram chat as an audio message with clickable article links.

No cloud APIs. No subscriptions. No GPU required.

---

## How it works

Each cycle Newscaster:

1. Fetches articles from configured RSS feeds
2. Deduplicates against a local SQLite database
3. Filters articles by topic keywords
4. Loads a local GGUF model, generates a prose bulletin, then immediately unloads it
5. Loads piper-tts, synthesises the bulletin to MP3, then immediately unloads it
6. Sends one Telegram message — audio with caption and clickable "Read more" links
7. Sleeps until the next cycle

The LLM and TTS engine are never resident in RAM between cycles, keeping memory usage low enough for an 8 GB laptop.

---

## Requirements

- Docker and Docker Compose
- Make
- A Telegram bot token and chat ID (from [@BotFather](https://t.me/botfather))
- ~2 GB disk space for models

No GPU. No AVX-512. Tested on Ubuntu Server 24.04 with an Intel CPU.

---

## Quick start

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_USERNAME/newscaster
cd newscaster

# 2. Run first-time setup — creates dirs, .env template, and builds the image
make setup

# 3. Edit .env — add your Telegram credentials
nano .env

# 4. Download a TTS voice
make download-voice

# 5. Download the LLM (recommended: 1.5B, ~1 GB)
make download-llm

# 6. Start the bot
make start

# 7. Watch the logs
make logs
```

The first bulletin will arrive within seconds of startup.

---

## Project structure

```
newscaster/
├── Dockerfile              # Multistage build — compiler in builder, clean runtime
├── docker-compose.yml      # Service definition with bind-mounted volumes
├── Makefile                # All common tasks
├── requirements.txt
├── .env                    # Your credentials (never commit this)
├── .env.example            # Template
├── config/
│   └── feeds.yaml          # All runtime configuration
├── models/
│   ├── tts/                # Piper voice model (.onnx + .onnx.json)
│   └── llm/                # GGUF model file
├── data/
│   └── audio/              # Cached MP3 files
├── scripts/
│   └── benchmark_llm.py    # LLM comparison tool
└── src/
    ├── config.py           # Typed Config dataclass — single source of truth
    ├── main.py             # Orchestrator and run loop
    ├── bot.py              # Telegram bot, /status command, caption formatting
    ├── database.py         # SQLite — deduplication, audio cache, pruning
    ├── fetcher.py          # RSS fetching with failure tracking
    ├── pipeline.py         # Topic filtering, cleaning, LLM digest generation
    ├── tts.py              # Piper TTS with preprocessing and audio caching
    └── logger.py           # Logging configuration
```

---

## Configuration

All runtime settings live in `config/feeds.yaml`. Changes take effect on the next container restart — no rebuild needed.

```yaml
feeds:
  - name: "Al Jazeera"
    url: "https://www.aljazeera.com/xml/rss/all.xml"
    enabled: true

fetch_interval: 30          # Minutes between cycles

max_articles_per_cycle: 10  # Cap after topic filtering
min_articles_per_cycle: 3   # Skip cycle if fewer articles match

topics:
  - war
  - artificial intelligence
  - climate
  - economy
  - iran
  - politics
  - trade

tts:
  enabled: true
  model_path: /models/tts/en_GB-jenny_dioco-medium.onnx
  cache_audio: true
  bitrate: "64k"

llm:
  model_path: /models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf
  max_tokens: 300
  temperature: 0.3

audio_cache_max_age_days: 30
article_retention_days: 7   # How long sent-article records are kept
```

Two environment variables are required in `.env`:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

---

## Makefile reference

| Command | Description |
|---|---|
| `make setup` | First-time setup — creates directories, copies `.env` template, builds image |
| `make build` | Rebuild the Docker image |
| `make start` | Start the bot in the background |
| `make stop` | Stop the bot |
| `make logs` | Tail live logs |
| `make reset-db` | Delete the database and audio cache (irreversible) |
| `make download-voice` | Download the default piper TTS voice |
| `make download-llm` | Download Qwen2.5-1.5B Q4_K_M (~1 GB, recommended) |
| `make download-llm-small` | Download Qwen2.5-0.5B Q4_K_M (~400 MB, for benchmarking) |
| `make test-llm` | Benchmark all present models and compare output quality and speed |

---

## Choosing a model

Two models have been tested. Run `make test-llm` to benchmark both on your hardware.

| Model | Size | Inference time (8 articles, CPU) | Output quality |
|---|---|---|---|
| Qwen2.5-1.5B-Instruct Q4_K_M | ~1 GB | ~50s | Good prose bulletin |
| Qwen2.5-0.5B-Instruct Q4_K_M | ~400 MB | ~22s | Headlines only — not recommended |

The 1.5B model reliably follows the prose bulletin format. The 0.5B is faster but ignores the prompt instructions and outputs reformatted headlines instead of written sentences. Unless RAM is severely constrained, use the 1.5B.

---

## Telegram commands

| Command | Description |
|---|---|
| `/start` | Confirm the bot is running |
| `/help` | List available commands |
| `/status` | Show uptime, last cycle, 24h article count, feed health, and model names |

---

## Docker architecture

The Dockerfile uses a two-stage build:

- **builder** — installs `build-essential` and `cmake`, compiles `llama-cpp-python` from source (CPU-only, no AVX assumptions), collects all Python dependencies into `/opt/venv`
- **runtime** — starts from a clean `python:3.12-slim`, copies only the venv, piper binary, and application source; no build tools present

This means code-only changes (`src/`) rebuild in seconds — only the final `COPY` layer changes and everything else is cached.

Models, audio, and configuration are bind-mounted at runtime and never baked into the image:

| Host path | Container path | Purpose |
|---|---|---|
| `./data` | `/data` | SQLite database and MP3 cache |
| `./config` | `/config` | `feeds.yaml` (read-only) |
| `./models/tts` | `/models/tts` | Piper voice model (read-only) |
| `./models/llm` | `/models/llm` | GGUF model file (read-only) |

---

## Feed failure handling

The fetcher tracks consecutive failures per feed in memory. After three consecutive failures a feed is suspended for the remainder of that cycle and a warning is logged. The counter resets automatically on the next successful fetch. Feed health is visible in the `/status` command.

---

## Storage pruning

Pruning runs automatically once every 24 hours inside the container — no cron job required. Two jobs run:

- Audio files and cache rows older than `audio_cache_max_age_days` (default 30) are deleted
- Sent-article records older than `article_retention_days` (default 7) are removed

---

## TTS preprocessing

Before synthesis, the digest text passes through a preprocessing pipeline that improves piper's pronunciation and prosody:

- Strips leaked markdown formatting (`**bold**`, `_italic_`, `# headings`)
- Expands abbreviations piper reads letter-by-letter (`U.S.` → `US`, `vs.` → `versus`)
- Rewrites ambiguous homographs in news context (`LIVE` → `live updates on`, `has read` → `has red`)
- Strips numbered lists (`1. `, `2. `) that the LLM occasionally produces
- Inserts natural pause punctuation after introductory adverbials

New rules can be added to `_PREPROCESS_RULES` in `src/tts.py` as `(pattern, replacement)` tuples without touching any control flow.

---

## Telegram caption limits

Telegram enforces a 1024-character limit on audio message captions and 4096 characters on text messages. The bot manages this budget automatically:

1. Tries to fit the full bulletin and article links within the limit
2. If over budget, drops the links section first
3. If still over budget, trims story bullets from the bottom and appends "… and N more"

The header and closing phrase are never dropped.

---

## Benchmarking

```bash
# Benchmark all models present in models/llm/
make test-llm

# Benchmark a specific model directly
docker compose run --rm newscaster \
  python scripts/benchmark_llm.py --model /models/llm/your-model.gguf

# Benchmark with your own article set
docker compose run --rm newscaster \
  python scripts/benchmark_llm.py \
  --model /models/llm/your-model.gguf \
  --articles /path/to/articles.json
```

The benchmark uses eight built-in sample articles covering all default topic areas if no `--articles` file is supplied. Output includes the full digest text, word count, and wall-clock inference time.

---

## License

MIT
