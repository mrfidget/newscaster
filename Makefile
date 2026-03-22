.PHONY: setup build download-voice download-llm download-llm-small \
        start stop logs reset-db test-llm help

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VOICE_MODEL  ?= en_GB-jenny_dioco-medium
VOICE_URL     = https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/jenny_dioco/medium

LLM_1B_FILE  = qwen2.5-1.5b-instruct-q4_k_m.gguf
LLM_05B_FILE = qwen2.5-0.5b-instruct-q4_k_m.gguf
LLM_1B_URL   = https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/$(LLM_1B_FILE)
LLM_05B_URL  = https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/$(LLM_05B_FILE)

# ---------------------------------------------------------------------------
# First-time setup — the only command a new user needs to run
# ---------------------------------------------------------------------------

setup: ## First-time setup: create dirs, copy .env template, build image
	@echo "── Creating host directories ────────────────────────────────────"
	@mkdir -p data/audio models/tts models/llm config scripts
	@if [ ! -f config/feeds.yaml ] && [ -f feeds.yaml ]; then \
	  cp feeds.yaml config/feeds.yaml; \
	  echo "Copied feeds.yaml → config/feeds.yaml"; \
	fi
	@echo "── Creating .env ────────────────────────────────────────────────"
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  echo "Created .env from .env.example"; \
	else \
	  echo ".env already exists — skipping"; \
	fi
	@echo "── Building Docker image ────────────────────────────────────────"
	docker compose build
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════════╗"
	@echo "║  Setup complete!  Next steps:                                ║"
	@echo "║                                                              ║"
	@echo "║  1. Edit .env — add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID  ║"
	@echo "║  2. Download a TTS voice:   make download-voice              ║"
	@echo "║  3. Download an LLM:        make download-llm                ║"
	@echo "║     (or the smaller model:  make download-llm-small)         ║"
	@echo "║  4. Start the bot:          make start                       ║"
	@echo "╚══════════════════════════════════════════════════════════════╝"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build: ## Rebuild the Docker image (code-only changes skip the builder stage)
	docker compose build

# ---------------------------------------------------------------------------
# Model downloads
# ---------------------------------------------------------------------------

download-voice: ## Download the default piper TTS voice (en_GB jenny_dioco medium)
	@mkdir -p models/tts
	curl -L "$(VOICE_URL)/$(VOICE_MODEL).onnx"      -o models/tts/$(VOICE_MODEL).onnx
	curl -L "$(VOICE_URL)/$(VOICE_MODEL).onnx.json" -o models/tts/$(VOICE_MODEL).onnx.json
	@echo "Voice model saved to models/tts/"

download-llm: ## Download Qwen2.5-1.5B Q4_K_M GGUF (~1 GB, recommended)
	@mkdir -p models/llm
	curl -L "$(LLM_1B_URL)" -o models/llm/$(LLM_1B_FILE)
	@echo "LLM saved to models/llm/$(LLM_1B_FILE)"

download-llm-small: ## Download Qwen2.5-0.5B Q4_K_M GGUF (~400 MB, for benchmarking)
	@mkdir -p models/llm
	curl -L "$(LLM_05B_URL)" -o models/llm/$(LLM_05B_FILE)
	@echo "LLM saved to models/llm/$(LLM_05B_FILE)"
	@echo "Run 'make test-llm' to compare it against the 1.5B model."

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

start: ## Start the bot in the background
	docker compose up -d

stop: ## Stop the bot
	docker compose down

logs: ## Tail live logs
	docker compose logs -f newscaster

reset-db: ## Delete the SQLite DB and audio cache — IRREVERSIBLE
	@read -p "Delete all data? This cannot be undone. [y/N] " confirm; \
	  [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ] && \
	  rm -f data/newscaster.db && \
	  rm -f data/audio/*.mp3 data/audio/*.wav && \
	  echo "Database and audio cache cleared." || echo "Aborted."

# ---------------------------------------------------------------------------
# Benchmarking
# ---------------------------------------------------------------------------

test-llm: ## Benchmark LLM(s) — runs scripts/benchmark_llm.py for each present model
	@echo "── LLM benchmark ───────────────────────────────────────────────"
	@MODELS="$(LLM_1B_FILE) $(LLM_05B_FILE)"; \
	 FOUND=0; \
	 for m in $$MODELS; do \
	   if [ -f "models/llm/$$m" ]; then \
	     FOUND=1; \
	     echo ""; \
	     echo "Testing: $$m"; \
	     docker compose run --rm --no-deps newscaster \
	       python scripts/benchmark_llm.py --model "/models/llm/$$m"; \
	   else \
	     echo "Skipping $$m (not present)"; \
	   fi; \
	 done; \
	 if [ "$$FOUND" = "0" ]; then \
	   echo "No models found. Run 'make download-llm' or 'make download-llm-small' first."; \
	 fi

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'