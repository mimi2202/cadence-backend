import os
import platform
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL   = os.environ.get("DATABASE_URL", "postgresql://localhost/cadence")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
UNSUB_SECRET   = os.environ.get("UNSUB_SECRET", "dev-only-change-me").encode()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENER_MODEL      = os.environ.get("OPENER_MODEL", "claude-sonnet-4-6")

WORKER_ID   = os.environ.get("WORKER_ID", platform.node() or "worker-1")
POLL_IDLE_S = float(os.environ.get("POLL_IDLE_S", "5"))
IMAP_POLL_S = float(os.environ.get("IMAP_POLL_S", "120"))

# Bounce/complaint thresholds that trip a mailbox's breaker.
MAX_BOUNCES_24H    = int(os.environ.get("MAX_BOUNCES_24H", "8"))
MAX_COMPLAINTS_24H = int(os.environ.get("MAX_COMPLAINTS_24H", "2"))