"""
Every number I chose lives here, so it is easy to find and change.
The limit numbers are starting guesses; probe_groq.py measures the real ones
and writes them to data/calibration.json, which overrides the defaults below.
"""
import json
import os
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------- models
GROQ_URL = "https://api.groq.com/openai/v1"
STANDIN_URL = "http://127.0.0.1:8100/openai/v1"
# The Llama models answered 404 "model does not exist" on this account (probe, 2026-11-11).
# The free tier lists gpt-oss-20b, gpt-oss-120b and qwen3.8-27b, all with the same limits.
MODEL_UNDERSTAND = "openai/gpt-oss-20b"     # small call: what is being asked
MODEL_DRAFT = "openai/gpt-oss-20b"          # writes the cited answer
ALT_MODEL = "qwen/qwen3.8-27b"              # measured side by side in the probe

# gpt-oss "thinks" before it answers. Thinking tokens count against max_tokens, so if
# max_tokens only fits the answer, the answer comes back empty. Give thinking its own room.
MODEL_PARAMS = {
    "openai/gpt-oss-20b": {"reasoning_effort": "low"},
    "openai/gpt-oss-120b": {"reasoning_effort": "low"},
    "qwen/qwen3.8-27b": {"reasoning_effort": "none"},
}
REASONING_HEADROOM = {"openai/gpt-oss-20b": 400, "openai/gpt-oss-120b": 400}

# ---------------------------------------------------------------- what "fast" means (my choice)
PATIENCE_S = 10.0          # a person gives up if no first token within 10 s
DRAFT_RESERVE_S = 2.5      # need at least this much time left to start drafting
CALL_READ_TIMEOUT_S = 6.0  # a model call that is silent this long is treated as stuck
OPTIONAL_STEP_TIMEOUT_S = 3.0  # the "understand" step is optional: if it is slow, go on without it
ANSWER_MAX_TOKENS = 300    # keep answers short: faster, cheaper, fewer places to go wrong
LEAN_MAX_TOKENS = 200

# ---------------------------------------------------------------- retries
MAX_RETRIES_429 = 2        # only if retry-after still fits before the deadline
MAX_RETRIES_ERROR = 1      # 5xx / connection error / stuck call, before first token only

# ---------------------------------------------------------------- daily budget
LEAN_WHEN_DAILY_LEFT = 0.20   # below 20% of the day's budget: skip optional steps
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily_usage.json")

# ---------------------------------------------------------------- limits (defaults, overwritten by calibration)
LIMITS = {
    "rpm": 30,          # requests per minute
    "tpm": 8000,        # tokens per minute      (x-ratelimit-limit-tokens header said 8000)
    "rpd": 1000,        # requests per day       (x-ratelimit-limit-requests header said 1000)
    "tpd": 200000,      # tokens per day         (not in any header; Groq docs)
}
SAFETY = 0.9            # use only 90% of a limit, the rest absorbs estimation error
TPM_COUNTS_MAX_TOKENS = True   # does the provider count max_tokens against TPM up front?
                               # probe_groq.py tests this; calibration.json overrides it

# price used for "cost per 1,000 answers" (USD per million tokens, gpt-oss-20b list price);
# the free tier itself costs $0
PRICE_IN_PER_M = 0.075
PRICE_OUT_PER_M = 0.30

# ---------------------------------------------------------------- retrieval settings (Table 3)
RETRIEVAL_SETTINGS = {
    #  name: (words per piece, pieces fetched, re-rank?)
    "S1": (120, 6, False),
    "S2": (250, 4, False),
    "S3": (250, 4, True),
    "S4": (500, 3, False),
}
DEFAULT_SETTING = "S1"    # chosen from Table 3 (most accurate, cheapest); the load runs used S2
LEAN_SETTING = "S1"       # used when the daily budget is low: fewer prompt tokens
WEAK_RETRIEVAL_SCORE = 1.0  # top BM25 score per query word; below this AND judged off-topic = refuse early


def call_settings(model, max_tokens):
    """max_tokens plus room for thinking, and the model's extra request fields."""
    return max_tokens + REASONING_HEADROOM.get(model, 0), MODEL_PARAMS.get(model, {})


def load_calibration():
    """Overwrite defaults with what probe_groq.py measured on the real service."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "calibration.json")
    global TPM_COUNTS_MAX_TOKENS
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cal = json.load(f)
        for k in LIMITS:
            if cal.get(k):
                LIMITS[k] = cal[k]
        if cal.get("tpm_counts_max_tokens") is not None:
            TPM_COUNTS_MAX_TOKENS = cal["tpm_counts_max_tokens"]
        return cal
    return {}


# ---------------------------------------------------------------- daily token budget (shared by all runs)
# The daily limit does NOT reset at midnight. Groq's header said requests come back one at a
# time: after 55 of 1000 requests, "x-ratelimit-reset-requests: 1h19m12s" = 55 x 86.4 s.
# I assume tokens per day work the same way (no header shows them): they refill at
# tpd / 86400 per second, about 2.3 tokens/s for 200k.
def daily_tokens_left(tpd):
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return min(tpd, state["left"] + (time.time() - state["at"]) * tpd / 86400)
    except (OSError, ValueError, KeyError):
        return tpd


def spend_daily_tokens(tokens, tpd):
    left = daily_tokens_left(tpd) - tokens
    os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump({"left": round(left), "at": time.time(),
                   "at_readable": datetime.now(timezone.utc).isoformat(timespec="seconds")}, f, indent=2)
