"""Runtime configuration for the CloudTrim audit engine.

Everything is environment-driven so the same code runs unchanged in:
  - demo mode  (docker-compose: moto at http://moto:4566)
  - sandbox CI (moto at http://127.0.0.1:4566)
  - live mode  (real AWS; no AWS_ENDPOINT_URL, standard credential chain)
"""
from __future__ import annotations

import os
from pathlib import Path

def _resolve_engine_dir() -> Path:
    """Locate the engine root (the dir holding terraform/, data/, output/).

    Works in three layouts:
      - source checkout:  engine/cloudtrim/config.py -> engine/
      - docker image:     package in site-packages, terraform/ at WORKDIR (/app)
      - explicit override via CLOUDTRIM_ENGINE_DIR (CI, custom installs)
    """
    env_dir = os.environ.get("CLOUDTRIM_ENGINE_DIR")
    if env_dir:
        return Path(env_dir)
    here = Path(__file__).resolve().parent.parent
    if (here / "terraform" / "seed").exists():
        return here
    for candidate in (Path.cwd(), Path("/app")):
        if (candidate / "terraform" / "seed").exists():
            return candidate
    return here


ENGINE_DIR = _resolve_engine_dir()
DATA_DIR = Path(os.environ.get("CLOUDTRIM_DATA_DIR", ENGINE_DIR / "data"))
OUTPUT_DIR = Path(os.environ.get("CLOUDTRIM_OUTPUT_DIR", ENGINE_DIR / "output"))
CUR_DIR = DATA_DIR / "cur"
TF_DIR = ENGINE_DIR / "terraform"
TF_REMEDIATION_DIR = OUTPUT_DIR / "terraform"

DB_PATH = Path(os.environ.get("CLOUDTRIM_DB", DATA_DIR / "cloudtrim.db"))
REPORT_PATH = OUTPUT_DIR / "cloudtrim-audit-report.pdf"

# --- AWS connection -------------------------------------------------------
# In demo/sandbox mode AWS_ENDPOINT_URL points at the emulator
# (LocalStack in docker-compose, moto in CI). In live mode it is unset and
# boto3 falls back to the real AWS endpoint + standard credential chain.
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
AWS_PROFILE = os.environ.get("AWS_PROFILE") or None

HOURS_PER_MONTH = 730.0
DAYS_PER_MONTH = 30.42

# Audit lookback windows
CW_LOOKBACK_DAYS = int(os.environ.get("CLOUDTRIM_CW_LOOKBACK_DAYS", "14"))
CUR_WINDOW_DAYS = int(os.environ.get("CLOUDTRIM_CUR_WINDOW_DAYS", "60"))

# Rule thresholds (documented in the PRD rules catalog)
IDLE_CPU_PCT = 5.0            # below -> idle candidate (stop)
RIGHTSIZE_CPU_PCT = 20.0      # below -> rightsizing candidate
SNAPSHOT_STALE_DAYS = 45      # billed longer than this -> stale
VOLUME_SNAPSHOT_GUARD_GB = 100  # snapshot-before-delete for unattached vols >= this
S3_LIFECYCLE_MIN_MONTHLY = 10.0  # only flag buckets costing more than this
LOG_RETENTION_DAYS = 90

# Demo seeding knobs (single source of truth for the wasteful account)
DEMO_CUR_PREFIX = "cloudtrim-demo-cur"
DEMO_TAG_PREFIX = "cloudtrim"

for _d in (DATA_DIR, CUR_DIR, OUTPUT_DIR, TF_REMEDIATION_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def is_live_mode() -> bool:
    """True when the engine is pointed at real AWS (no emulator endpoint)."""
    return AWS_ENDPOINT_URL is None


def mode_label() -> str:
    endpoint = AWS_ENDPOINT_URL or "https://aws.amazon.com (LIVE)"
    return f"endpoint={endpoint} region={AWS_REGION} live={is_live_mode()}"
