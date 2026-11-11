"""
Learn how the real Groq service behaves, with a modest number of requests.

  A. limits per model, read from the headers Groq sends back          (3 requests)
  B. latency spread on a realistic answer-drafting prompt              (2 x 10 requests, paced)
  C. does Groq count max_tokens against tokens-per-minute up front?    (3 requests)
  D. many requests in flight at once                                    (10 at the same time)
  E. hit the requests-per-minute limit on purpose, then time recovery  (~35 tiny requests)
  F. hit the tokens-per-minute limit on purpose, then time recovery    (5 big requests at once)

B runs on the main model AND on config.ALT_MODEL (10 each), so the model choice follows
from measurements. Uses roughly 40k tokens of the main model's daily budget (plus about
20k of the other model's). Takes about 15 minutes.

Run:   set GROQ_API_KEY=your_key      (PowerShell: $env:GROQ_API_KEY="your_key")
       python probe_groq.py
Writes data/probe_real.csv (one row per request), data/probe_429.json (what a
limit looks like) and data/calibration.json (numbers the stand-in copies).
"""
import csv
import sys
import json
import os
import statistics
import threading
import time

import config

sys.stdout.reconfigure(errors="replace")   # Windows consoles cannot print every character
from llm_client import CallTimeout, RateLimited, ServerError, stream_chat
from pipeline import DRAFT_SYSTEM, Limiter
from retriever import Retriever

KEY = os.environ.get("GROQ_API_KEY")
if not KEY:
    raise SystemExit("Set GROQ_API_KEY first (see the top of this file).")

MODEL = config.MODEL_DRAFT
CANDIDATES = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
rows, examples_429 = [], []
lock = threading.Lock()


def call(test, model, messages, max_tokens, realistic=False):
    """One real request; always returns a row, never raises.
    realistic=True: same settings as the pipeline (room for thinking added to max_tokens)."""
    extra = config.MODEL_PARAMS.get(model, {})
    if realistic:
        max_tokens, extra = config.call_settings(model, max_tokens)
    row = {"test": test, "model": model, "sent_at": time.strftime("%H:%M:%S"), "status": "ok",
           "ttft_ms": "", "thinking_ms": "", "total_ms": "", "tokens_in": "", "tokens_out": "",
           "answer_chars": "", "finish_reason": "", "max_tokens": max_tokens,
           "retry_after": "", "limit_requests_day": "", "remaining_requests_day": "",
           "limit_tokens_min": "", "remaining_tokens_min": "", "reset_tokens": "", "message": ""}
    t0 = time.monotonic()
    try:
        res = stream_chat(config.GROQ_URL, KEY, model, messages, max_tokens, read_timeout=30, extra=extra)
        h = res.headers
        thinking = (res.ttft_ms - res.thinking_started_ms) if res.ttft_ms and res.thinking_started_ms else ""
        row.update(ttft_ms=round(res.ttft_ms or res.total_ms), total_ms=round(res.total_ms),
                   thinking_ms=round(thinking) if thinking != "" else "",
                   tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                   answer_chars=len(res.text), finish_reason=res.finish_reason or "")
    except RateLimited as e:
        h = e.headers
        row.update(status="rate_limited", retry_after=e.retry_after, message=e.message[:300],
                   total_ms=round((time.monotonic() - t0) * 1000))
        with lock:
            examples_429.append({"test": test, "message": e.message, "headers": e.headers})
    except (ServerError, CallTimeout) as e:
        h = {}
        row.update(status="error", message=str(e)[:300], total_ms=round((time.monotonic() - t0) * 1000))
    row.update(limit_requests_day=h.get("x-ratelimit-limit-requests", ""),
               remaining_requests_day=h.get("x-ratelimit-remaining-requests", ""),
               limit_tokens_min=h.get("x-ratelimit-limit-tokens", ""),
               remaining_tokens_min=h.get("x-ratelimit-remaining-tokens", ""),
               reset_tokens=h.get("x-ratelimit-reset-tokens", ""))
    with lock:
        rows.append(row)
    return row


def tiny(text="Reply with the single word OK."):
    return [{"role": "user", "content": text}]


def realistic(question):
    """The same kind of prompt the pipeline sends when drafting an answer."""
    hits, _ = retriever.search(question)
    passages = "\n\n".join(f"[P{i}] ({h['paper_name']}, page {h['page']}, section: {h['section']})\n{h['text']}"
                           for i, h in enumerate(hits, 1))
    return [{"role": "system", "content": DRAFT_SYSTEM},
            {"role": "user", "content": f"Passages:\n{passages}\n\nQuestion: {question}"}]


def at_once(n, fn):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def wait_for_fresh_minute():
    print("   waiting 62 s so the per-minute window is empty ...")
    time.sleep(62)


def recover(test, model, messages, max_tokens, retry_after, realistic=False):
    """After a 429: wait until just before retry-after says, then try every 0.5 s.
    Returns seconds until a request worked (tells us if retry-after is honest)."""
    t0 = time.monotonic()
    time.sleep(max(0.0, float(retry_after or 0) - 1.0))
    while time.monotonic() - t0 < 120:
        if call(test + "_recovery", model, messages, max_tokens, realistic)["status"] == "ok":
            return round(time.monotonic() - t0, 1)
        time.sleep(0.5)
    return None


def save_raw():
    """Saved after every part, so a crash later never loses what was already measured."""
    with open("data/probe_real.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open("data/probe_429.json", "w", encoding="utf-8") as fh:
        json.dump(examples_429, fh, indent=2)


retriever = Retriever(*config.RETRIEVAL_SETTINGS[config.DEFAULT_SETTING])
questions = [q["question"] for q in json.load(open("questions.json", encoding="utf-8"))]
os.makedirs("data", exist_ok=True)

print("A. limits per model (from response headers)")
ok_models, tpm_of = [], {}
for m in CANDIDATES:
    r = call("A_headers", m, tiny(), 5)
    print(f"   {m}: status={r['status']} requests/day={r['limit_requests_day']} tokens/min={r['limit_tokens_min']}"
          + (f"  ({r['message'][:90]})" if r["status"] != "ok" else ""))
    if r["status"] == "ok":
        ok_models.append(m)
        tpm_of[m] = int(r["limit_tokens_min"]) if str(r["limit_tokens_min"]).isdigit() else config.LIMITS["tpm"]
save_raw()
if MODEL not in ok_models:
    raise SystemExit(f"\nThe main model {MODEL} did not answer. Stopping here so no budget is wasted. "
                     f"Models that answered: {ok_models}. Send this output to Claude.")

compare = [m for m in (MODEL, config.ALT_MODEL) if m in ok_models]
print(f"B. latency on realistic answer prompts: 10 each on {compare}, spaced to stay under the limits")
pacers = {m: Limiter(rpm=30, tpm=tpm_of[m]) for m in compare}   # same limiter the pipeline uses
for i in range(10):
    msgs = realistic(questions[(2 * i) % len(questions)])      # every other question: all papers
    for m in compare:                                            # each model has its own limits
        max_t, _ = config.call_settings(m, config.ANSWER_MAX_TOKENS)
        pacers[m].acquire(sum(len(x["content"]) for x in msgs) // 4 + max_t, deadline=time.time() + 120)
        r = call("B_latency", m, msgs, config.ANSWER_MAX_TOKENS, realistic=True)
        print(f"   {i + 1:2d} {m}: {r['status']} first answer token {r['ttft_ms']} ms "
              f"(thinking {r['thinking_ms'] or 0} ms), total {r['total_ms']} ms, tokens {r['tokens_in']}+"
              f"{r['tokens_out']}, answer {r['answer_chars']} chars, stop: {r['finish_reason']}")
save_raw()
for m in compare:
    b = [r for r in rows if r["test"] == "B_latency" and r["model"] == m and r["status"] == "ok"]
    if b:
        print(f"   {m}: median first answer token {statistics.median(r['ttft_ms'] for r in b):.0f} ms, "
              f"median output tokens {statistics.median(r['tokens_out'] for r in b):.0f}, "
              f"empty answers {sum(1 for r in b if not r['answer_chars'])}/{len(b)}")

print("C. does max_tokens count against tokens/minute up front?")
wait_for_fresh_minute()
c1 = call("C_max5", MODEL, tiny(), 5)
c2 = call("C_max3000", MODEL, tiny(), 3000)
c3 = call("C_max5_again", MODEL, tiny(), 5)


def remaining(r):
    return int(r["remaining_tokens_min"]) if str(r["remaining_tokens_min"]).isdigit() else None


drop_big = drop_small = counts_max = None
if all(c["status"] == "ok" and remaining(c) is not None for c in (c1, c2, c3)):
    drop_big = remaining(c1) - remaining(c2)
    drop_small = remaining(c2) - remaining(c3)
    counts_max = drop_big > 1000
print(f"   tokens/minute left dropped by {drop_big} for max_tokens=3000, by {drop_small} for max_tokens=5"
      f" -> counts max_tokens up front: {counts_max}")
save_raw()

print("D. 10 requests in flight at the same time")
wait_for_fresh_minute()
at_once(10, lambda i: call("D_concurrent", MODEL, tiny(), 5))
d = [r for r in rows if r["test"] == "D_concurrent"]
print(f"   ok: {sum(r['status'] == 'ok' for r in d)}/10, "
      f"first-token times: {sorted(r['ttft_ms'] for r in d if r['ttft_ms'] != '')}")
save_raw()

print("E. requests-per-minute limit: tiny requests back to back until a 429")
wait_for_fresh_minute()
first_429_at = None
for i in range(1, 46):
    r = call("E_rpm", MODEL, tiny(), 1)
    if r["status"] == "rate_limited":
        first_429_at = i
        print(f"   request {i} got 429: {r['message'][:160]}")
        print(f"   retry-after header: {r['retry_after']} s")
        break
rpm_retry_after = r["retry_after"] if first_429_at else None
rpm_recovery = recover("E_rpm", MODEL, tiny(), 1, rpm_retry_after) if first_429_at else None
print(f"   recovered after {rpm_recovery} s" if first_429_at else "   no 429 within 45 requests")
save_raw()

print("F. tokens-per-minute limit: 5 big requests at once")
wait_for_fresh_minute()
big = realistic(questions[0])
at_once(5, lambda i: call("F_tpm", MODEL, big, config.ANSWER_MAX_TOKENS, realistic=True))
f = [r for r in rows if r["test"] == "F_tpm"]
f429 = [r for r in f if r["status"] == "rate_limited"]
print(f"   {len(f429)}/5 got 429. Example: {f429[0]['message'][:200] if f429 else '-'}")
tpm_retry_after = f429[0]["retry_after"] if f429 else None
tpm_recovery = recover("F_tpm", MODEL, big, config.ANSWER_MAX_TOKENS, tpm_retry_after,
                       realistic=True) if f429 else None
print(f"   recovered after {tpm_recovery} s")

# ------------------------------------------------------------------ save raw results + calibration
save_raw()

b_ok = [r for r in rows if r["test"] == "B_latency" and r["status"] == "ok" and r["model"] == MODEL]
ttfts = sorted(r["ttft_ms"] for r in b_ok)
# streaming speed of the visible answer, after its first token (about 4 characters per token)
speeds = [(r["answer_chars"] / 4) / ((r["total_ms"] - r["ttft_ms"]) / 1000)
          for r in b_ok if r["answer_chars"] and r["answer_chars"] > 80 and r["total_ms"] > r["ttft_ms"]]
main_rows = [r for r in rows if r["model"] == MODEL]


def summary(model):
    b = [r for r in rows if r["test"] == "B_latency" and r["model"] == model and r["status"] == "ok"]
    if not b:
        return None
    return {"requests": len(b),
            "first_answer_token_median_ms": statistics.median(r["ttft_ms"] for r in b),
            "thinking_median_ms": statistics.median(r["thinking_ms"] or 0 for r in b),
            "output_tokens_median": statistics.median(r["tokens_out"] for r in b),
            "input_tokens_median": statistics.median(r["tokens_in"] for r in b),
            "empty_answers": sum(1 for r in b if not r["answer_chars"])}


def pct(xs, p):
    return xs[max(0, int(round(p / 100 * len(xs) + 0.5)) - 1)] if xs else None


model_rows = [r for r in rows if r["model"] == MODEL and r["limit_tokens_min"]]
cal = {
    "model": MODEL,
    "measured_at": time.strftime("%Y-%m-%d %H:%M"),
    "ttft_p50_ms": pct(ttfts, 50), "ttft_p95_ms": pct(ttfts, 95), "ttft_p99_ms": pct(ttfts, 99),
    "ttft_max_ms": ttfts[-1] if ttfts else None, "latency_requests": len(ttfts),
    "tokens_per_s": round(statistics.median(speeds)) if speeds else None,
    "tpm": int(model_rows[-1]["limit_tokens_min"]) if model_rows else None,
    "rpd": int(model_rows[-1]["limit_requests_day"]) if model_rows else None,
    "rpm": (first_429_at - 1) if first_429_at else None,
    "tpd": config.LIMITS["tpd"],   # not in any header: taken from the docs / console limits page
    "tpm_counts_max_tokens": counts_max,
    "tpm_drop_for_max_tokens_3000": drop_big, "tpm_drop_for_max_tokens_5": drop_small,
    "rpm_first_429_at_request": first_429_at, "rpm_retry_after_s": rpm_retry_after,
    "rpm_recovery_s": rpm_recovery,
    "tpm_429_count_of_5": len(f429), "tpm_retry_after_s": tpm_retry_after, "tpm_recovery_s": tpm_recovery,
    "concurrent_ok_of_10": sum(r["status"] == "ok" for r in d),
    "error_rate": round(sum(r["status"] == "error" for r in main_rows) / len(main_rows), 3),
    "requests_sent": len(main_rows),
    "tokens_used": sum(int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0) for r in main_rows),
    "compare": {m: summary(m) for m in compare},
}
with open("data/calibration.json", "w", encoding="utf-8") as fh:
    json.dump(cal, fh, indent=2)
config.spend_daily_tokens(cal["tokens_used"], config.LIMITS["tpd"])   # the daily budget is shared by every run
print("\nSaved data/probe_real.csv, data/probe_429.json, data/calibration.json")
print(json.dumps(cal, indent=2))
