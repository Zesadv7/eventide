"""Configuration, environment loading, and runtime paths."""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv


# Load environment variables from .env first.
load_dotenv(override=True)

# Anthropic-compatible providers may set a base URL.
if os.getenv("ANTHROPIC_BASE_URL"):
    # Avoid leaking an auth token when a custom base URL is used.
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# Working directory is the project root where the user runs the agent.
WORKDIR = Path.cwd()

# Anthropic client. The API key is read from ANTHROPIC_API_KEY automatically.
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL") or None)

# Model selection.
MODEL = os.environ.get("MODEL_ID", "claude-sonnet-4-6")
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# Runtime directories. These are created on demand by consumers.
SKILLS_DIR = WORKDIR / "skills"
TASKS_DIR = WORKDIR / ".tasks"
WORKTREES_DIR = WORKDIR / ".worktrees"
MAILBOX_DIR = WORKDIR / ".mailboxes"
MEMORY_DIR = WORKDIR / ".memory"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# LLM / retry constants.
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000

# Prompts / UI.
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36mnexus >> \033[0m"

# Set to True when the interactive CLI is active; background threads use it
# to decide whether to redraw the prompt line.
CLI_ACTIVE = False
