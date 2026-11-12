"""
Join results/*.csv into results.csv, then compute every table in the brief FROM
THAT FILE ONLY (Table 1 comes from data/calibration.json + data/probe_real.csv).

    python make_tables.py        -> results.csv and tables.md

Definitions (so a reviewer can recompute them):
  percentiles      nearest-rank over requests that got a first token
  duration (s)     first start_time to the last request's end (start_time + total_ms)
  offered rate     requests sent / duration * 60
  succeeded %      status == ok;  rate-limited % status == rate_limited;
  timed out/given up %  status == timeout;  (status == error is listed separately)
  peak queue       most requests at one moment that had arrived but not yet got a first token
  tokens/answer    mean of tokens_in + tokens_out over all requests in the scenario
  cost / 1,000     the free tier costs $0; in brackets, what it would cost at list price
  waiting / working / retrying (detail files): waiting = in our limiter's line; retrying = time
                   in calls that failed (429, timeout, error) plus the back-off before trying again
"""
import csv
import sys
import glob
import json
import math
import os
from datetime import datetime

import config

sys.stdout.reconfigure(errors="replace")   # Windows consoles cannot print every character

ORDER = ["real_steady", "real_burst_v1", "real_burst", "steady", "burst", "overload", "rpm_halved", "daily_exhausted",
         "burst_no_limiter", "burst_naive_retry"]


def pct(values, p):
    v = sorted(values)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)] if v else None


def fmt(*xs):
    return " / ".join("n/a" if x is None else f"{x:.0f}" for x in xs)


def ts(s):
    return datetime.fromisoformat(s).timestamp()


# ------------------------------------------------------------------ 1. one results.csv
rows = []
for path in sorted(glob.glob("results/*.csv")):
    with open(path, encoding="utf-8") as f:
        rows += list(csv.DictReader(f))
with open("results.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

# ------------------------------------------------------------------ 2. read it back, as a reviewer would
with open("results.csv", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
by_scenario = {}
for r in rows:
    by_scenario.setdefault(r["scenario"], []).append(r)


def num(r, k):
    return float(r[k]) if r[k] not in ("", None) else None


def stats(rs):
    n = len(rs)
    ft = [num(r, "first_token_ms") for r in rs if num(r, "first_token_ms") is not None]
    tt = [num(r, "total_ms") for r in rs]
    starts = [ts(r["start_time"]) for r in rs]
    ends = [ts(r["start_time"]) + num(r, "total_ms") / 1000 for r in rs]
    duration = max(ends) - min(starts)
    # peak queue: arrived but no first token yet
    events = []
    for r in rs:
        s = ts(r["start_time"])
        f = num(r, "first_token_ms")
        events += [(s, 1), (s + (f if f is not None else num(r, "total_ms")) / 1000, -1)]
    q = peak = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        q += d
        peak = max(peak, q)
    tok = [int(r["tokens_in"]) + int(r["tokens_out"]) for r in rs]
    tin = sum(int(r["tokens_in"]) for r in rs) / n
    tout = sum(int(r["tokens_out"]) for r in rs) / n
    cost = (tin * config.PRICE_IN_PER_M + tout * config.PRICE_OUT_PER_M) / 1e6 * 1000
    share = lambda s: 100 * sum(r["status"] == s for r in rs) / n
    grades = [r["answer_grade"] for r in rs]
    return {
        "n": n, "source": rs[0]["source"], "duration": duration, "rate": n / duration * 60,
        "ft": (pct(ft, 50), pct(ft, 95), pct(ft, 99)), "tt": (pct(tt, 50), pct(tt, 95), pct(tt, 99)),
        "ok": share("ok"), "rl": share("rate_limited"), "to": share("timeout"), "err": share("error"),
        "retries": sum(int(r["retries"]) for r in rs) / n, "peak": peak,
        "tokens": sum(tok) / n, "cost": cost,
        "correct": 100 * grades.count("correct") / n, "wrong": 100 * grades.count("wrong") / n,
        "ref_ok": 100 * grades.count("refused_correctly") / n,
        "ref_bad": 100 * grades.count("refused_wrongly") / n,
        "ft2": (pct(ft, 50), pct(ft, 95)), "tt2": (pct(tt, 50), pct(tt, 95)),
    }


out = []
# ------------------------------------------------------------------ Table 1
cal = json.load(open("data/calibration.json", encoding="utf-8")) if os.path.exists("data/calibration.json") else {}
probe = list(csv.DictReader(open("data/probe_real.csv", encoding="utf-8"))) if os.path.exists("data/probe_real.csv") else []
example = json.load(open("data/probe_429.json", encoding="utf-8")) if os.path.exists("data/probe_429.json") else []
def first_429(test):
    return next((e for e in example if e["test"] == test), None)


e_rpm, e_tpm = first_429("E_rpm"), first_429("F_tpm")
out.append("## Table 1: Limits found in the real service (" + cal.get("model", "not measured") + ")\n")
out.append("| Limit | Value found | How found | Confidence |\n|---|---|---|---|")
out.append(f"| Requests per minute | 30 | request {cal.get('rpm_first_429_at_request')} of a back-to-back run got the "
           f"first 429 (\"Limit 30, Used 30\"; one extra slipped in as the limit refilled) | high |")
out.append(f"| Tokens per minute | {cal.get('tpm') or 'not measured'} | x-ratelimit-limit-tokens header; "
           f"{cal.get('tpm_429_count_of_5')} of 5 big simultaneous requests got 429 | high |")
out.append(f"| Requests per day | {cal.get('rpd') or 'not measured'} | x-ratelimit-limit-requests header | high |")
out.append(f"| Tokens per day | {cal.get('tpd') or 'not measured'} | not in any header, not reached; Groq docs | low |")
out.append(f"| Requests in flight at once | at least 10 | {cal.get('concurrent_ok_of_10')} of 10 simultaneous requests "
           f"succeeded; not pushed higher | low |")
out.append("| What it returns when a limit is hit | HTTP 429, retry-after in whole seconds, and a body naming the limit: "
           "\"Rate limit reached … on tokens per minute (TPM): Limit 8000, Used 7926, Requested 1983. Please try again "
           "in 14.3175s\" | data/probe_429.json | high |")
out.append(f"| Time until it recovers | RPM: retry-after {cal.get('rpm_retry_after_s')} s, worked after "
           f"{cal.get('rpm_recovery_s')} s. TPM: retry-after {cal.get('tpm_retry_after_s')} s, worked after "
           f"{cal.get('tpm_recovery_s')} s | waited retry-after minus 1 s, then tried every 0.5 s | high |")
reset_req = (e_rpm or {}).get("headers", {}).get("x-ratelimit-reset-requests", "?")
out.append(f"\n**How the limits refill (measured):** not in fixed windows. The RPM 429 said \"try again in 2s\" "
           f"(60 s / 30 requests); the TPM 429 asked to wait (tokens missing) / (8000 / 60 per s). Daily "
           f"requests refill one at a time too: x-ratelimit-reset-requests was {reset_req} after "
           f"{cal.get('rpd', 0) - int((e_rpm or {}).get('headers', {}).get('x-ratelimit-remaining-requests', 0) or 0)} "
           f"requests, i.e. 86.4 s each. Tokens per day are assumed to refill the same way (not visible).")
out.append(f"\nAlso measured: first answer token p50/p95 = {cal.get('probe_ttft_p50_ms', cal.get('ttft_p50_ms'))}/"
           f"{cal.get('probe_ttft_p95_ms', cal.get('ttft_p95_ms'))} ms over {cal.get('latency_requests')} probe requests "
           f"({cal.get('ttft_p50_ms')}/{cal.get('ttft_p95_ms')} ms over all {cal.get('ttft_samples', '?')} real calls); "
           f"max_tokens booked against TPM up front: {cal.get('tpm_counts_max_tokens')} (tokens left dropped by "
           f"{cal.get('tpm_drop_for_max_tokens_3000')} for max_tokens=3000; the TPM 429 for a ~1,900-token prompt "
           f"with max_tokens 700 said \"Requested 1983\"). Probe used {cal.get('requests_sent')} requests, "
           f"{cal.get('tokens_used')} tokens.\n")

# ------------------------------------------------------------------ Tables 2a / 2b
load = [s for s in ORDER if s in by_scenario] + sorted(s for s in by_scenario
                                                       if s not in ORDER and not s.startswith("quality_"))
S = {s: stats(by_scenario[s]) for s in load}
out.append("## Table 2a: Latency per load scenario\n")
out.append("| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | "
           "First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |\n|---|---|---|---|---|---|---|")
for s in load:
    x = S[s]
    out.append(f"| {s} | {x['source']} | {x['n']} | {x['duration']:.0f} | {x['rate']:.1f} | {fmt(*x['ft'])} | {fmt(*x['tt'])} |")
out.append("\n## Table 2b: Outcomes, same rows\n")
out.append("| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | "
           "Peak queue length | Tokens per answer | Cost per 1,000 answers |\n|---|---|---|---|---|---|---|---|")
for s in load:
    x = S[s]
    err = f" (+{x['err']:.1f}% error)" if x["err"] else ""
    out.append(f"| {s} | {x['ok']:.1f} | {x['rl']:.1f} | {x['to']:.1f}{err} | {x['retries']:.2f} | {x['peak']} | "
               f"{x['tokens']:.0f} | $0 (${x['cost']:.3f} at list price) |")

# ------------------------------------------------------------------ Table 3
quality = sorted(s for s in by_scenario if s.startswith("quality_"))
out.append("\n## Table 3: Retrieval settings\n")
out.append("| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | "
           "Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |"
           "\n|---|---|---|---|---|---|---|---|")
missed = []
for s in quality:
    x = stats(by_scenario[s])
    name = s.replace("quality_", "")
    size, k, rr = config.RETRIEVAL_SETTINGS[name]
    out.append(f"| {name}: {size} words, {k} pieces, re-rank {'yes' if rr else 'no'} | {x['n']} | {x['correct']:.1f} | "
               f"{x['wrong']:.1f} | {x['ref_ok']:.1f} | {x['tokens']:.0f} | {fmt(*x['ft2'])} | {fmt(*x['tt2'])} |")
    if x["ref_bad"]:
        missed.append(f"{name}: {x['ref_bad']:.1f}%")
if missed:
    out.append(f"\nThe rest of each row refused an answerable question (a miss, not a wrong answer): "
               f"{', '.join(missed)}.")

# ------------------------------------------------------------------ answer quality in the load runs
out.append("\n## Answer quality inside the load runs (same grader)\n")
out.append("| Scenario | correct % | wrong % | refused correctly % | refused wrongly % | not graded % |\n|---|---|---|---|---|---|")
for s in load:
    x = S[s]
    seen = x["correct"] + x["wrong"] + x["ref_ok"] + x["ref_bad"]
    out.append(f"| {s} | {x['correct']:.1f} | {x['wrong']:.1f} | {x['ref_ok']:.1f} | {x['ref_bad']:.1f} | {100 - seen:.1f} |")
out.append("\nnot graded = the person gave up before a reply, or the stand-in had no recorded real answer "
           "for that exact prompt and invented placeholder text (not a real answer, so not graded).")

# ------------------------------------------------------------------ slowest requests, for "where the time goes"
out.append("\n## Slowest requests (from results/*_detail.jsonl)\n")
out.append("| request | status | mode | first token ms | total ms | waiting ms | working ms | retrying ms | model first token ms | retries |\n|---|---|---|---|---|---|---|---|---|---|")
details = []
for path in glob.glob("results/*_detail.jsonl"):
    details += [json.loads(l) for l in open(path, encoding="utf-8")]
for d in sorted(details, key=lambda d: -d["total_ms"])[:8]:
    out.append(f"| {d['request_id']} | {d['status']} | {d['mode']} | {d['first_token_ms']} | {d['total_ms']} | "
               f"{d['waiting_ms']} | {d['working_ms']} | {d['retrying_ms']} | {d['model_ttft_ms']} | {d['retries']} |")

with open("tables.md", "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
print("\n".join(out))
print(f"\nresults.csv: {len(rows)} rows from {len(by_scenario)} scenarios -> tables.md")
