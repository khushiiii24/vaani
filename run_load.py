"""
Run the pipeline under one load shape and save one row per request.

Stand-in scenarios (start the fake Groq automatically, no key needed):
    python run_load.py steady
    python run_load.py burst
    python run_load.py overload
    python run_load.py rpm_halved
    python run_load.py daily_exhausted
    python run_load.py burst_no_limiter
    python run_load.py burst_naive_retry

Real Groq scenarios (need GROQ_API_KEY):
    python run_load.py real_steady
    python run_load.py real_burst
    python run_load.py quality --setting S2      (every question once, for Table 3)

Writes results/<scenario>.csv (the brief's columns) and results/<scenario>_detail.jsonl
(the answer text and where the time went). make_tables.py joins them into results.csv.
"""
import argparse
import csv
import sys
import json
import os
import threading
import time
from datetime import datetime, timezone

import config

sys.stdout.reconfigure(errors="replace")   # Windows consoles cannot print every character
import standin_server
from pipeline import Relay

cal = config.load_calibration()
PAID = 10   # stand-in plays a paid plan: 10x every free-tier limit (per minute and per day)


def every(seconds, duration, start=0.0):
    t, out = start, []
    while t < start + duration:
        out.append(round(t, 2))
        t += seconds
    return out


def burst(n, at, spread=3.0):
    return [round(at + spread * i / n, 2) for i in range(n)]


# name: (source, arrival times in seconds, stand-in settings, pipeline settings, what it tests)
SCENARIOS = {
    "steady": ("stand-in", every(6, 180), {}, {}, "10/min, about a third of capacity (~30 answers/min: 72k usable tokens/min / ~2.4k per answer)"),
    "burst": ("stand-in", every(12, 120) + burst(60, 30), {}, {},
              "5/min background, then 60 requests within 3 s (an outage makes everyone ask at once)"),
    "overload": ("stand-in", every(1, 180), {}, {}, "60/min for 3 minutes, about 2x capacity"),
    "rpm_halved": ("stand-in", every(2, 180), {"limit_factor": 0.5}, {},
                   "30/min: about the normal capacity, 2x the halved one; provider halves limits without telling us"),
    "daily_exhausted": ("stand-in", every(6, 180), {"tpd": 40000}, {"tpd": 40000},
                        "daily token budget runs out part way through"),
    "burst_no_limiter": ("stand-in", burst(60, 0), {}, {"use_limiter": False},
                         "same burst, no client-side limiter, retries honour retry-after"),
    "burst_naive_retry": ("stand-in", burst(60, 0), {}, {"use_limiter": False, "retry_policy": "naive"},
                          "same burst, no limiter, retry at once up to 4 times"),
    "real_steady": ("real", every(30, 300), {}, {}, "2/min on real Groq for 5 minutes"),
    "real_burst": ("real", burst(10, 0, spread=1.0), {}, {}, "10 requests at once on real Groq"),
}

PAPER_NAMES = {"attention": "Attention Is All You Need", "moshi": "Moshi", "hnet": "Dynamic Chunking (H-Net)"}
COLUMNS = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status",
           "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade"]


def grade(q, out):
    """correct / wrong / refused_correctly / refused_wrongly.
    Blank if the person never saw a reply, or if the stand-in invented the text (nothing real to grade)."""
    if out["first_token_ms"] == "" or out["first_token_ms"] > config.PATIENCE_S * 1000:
        return ""
    if out["invented_text"] and out["mode"] in ("full", "lean"):
        return ""
    text = out["text"]
    low = text.lower().replace(",", "")
    facts = all(any(a.lower() in low for a in group) for group in q["must_include"])
    cites_right_paper = PAPER_NAMES.get(q["paper"], "~") in text
    if out["mode"] in ("refused", "refused_early"):
        return "refused_wrongly" if q["answerable"] else "refused_correctly"
    if out["mode"] == "passages_only":       # a refusal that quotes the closest passages
        if not q["answerable"]:
            return "refused_correctly"
        return "correct" if facts and cites_right_paper else "refused_wrongly"
    if not q["answerable"]:
        return "wrong"
    return "correct" if facts and cites_right_paper else "wrong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCENARIOS) + ["quality"])
    ap.add_argument("--setting", default=config.DEFAULT_SETTING, choices=list(config.RETRIEVAL_SETTINGS))
    a = ap.parse_args()
    questions = json.load(open("questions.json", encoding="utf-8"))

    if a.scenario == "quality":
        source, arrivals, si_cfg, relay_opts = "real", None, {}, {"allow_lean": False}
        name = f"quality_{a.setting}"
    else:
        source, arrivals, si_cfg, relay_opts, _ = SCENARIOS[a.scenario]
        name = a.scenario

    limits = dict(config.LIMITS)
    server = None
    if source == "real":
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise SystemExit("Set GROQ_API_KEY first.")
        base_url = config.GROQ_URL
    else:
        key = "stand-in"
        base_url = config.STANDIN_URL
        for k in ("rpm", "tpm", "rpd", "tpd"):
            limits[k] = limits[k] * PAID
        factor = si_cfg.pop("limit_factor", 1.0)
        server, si = standin_server.start({
            "rpm": int(limits["rpm"] * factor), "tpm": int(limits["tpm"] * factor),
            "tpd": si_cfg.get("tpd", limits["tpd"]), "rpd": limits["rpd"]})
    limits.update({k: v for k, v in relay_opts.items() if k in limits})
    opts = {k: v for k, v in relay_opts.items() if k not in limits}
    relay = Relay(base_url, key, limits, setting=a.setting, record_replay=(source == "real"), **opts)
    if source == "real":            # earlier runs today already used part of the daily budget
        relay.budget.tokens_left = config.daily_tokens_left(limits["tpd"])
        print(f"daily token budget left before this run: {relay.budget.tokens_left:.0f} of {limits['tpd']} "
              f"(it refills about {limits['tpd'] / 86400:.1f} tokens/s)")

    rows, details = [], []
    lock = threading.Lock()

    def one(i, q, arrival):
        out = relay.answer(q["question"], arrival)
        row = {
            "request_id": f"{name}-{i:04d}", "scenario": name, "source": source,
            "start_time": datetime.fromtimestamp(arrival, timezone.utc).isoformat(timespec="milliseconds"),
            "first_token_ms": out["first_token_ms"], "total_ms": out["total_ms"], "status": out["status"],
            "retries": out["retries"], "tokens_in": out["tokens_in"], "tokens_out": out["tokens_out"],
            "retrieval_setting": out["setting"] or a.setting, "answer_grade": grade(q, out),
        }
        with lock:
            rows.append(row)
            details.append({"request_id": row["request_id"], "question_id": q["id"], **out})
        print(f"  {row['request_id']} {q['id']:3s} {row['status']:12s} {out['mode']:13s} "
              f"first {row['first_token_ms']} ms  total {row['total_ms']} ms  {row['answer_grade']}")

    print(f"running {name} on {source}")
    if arrivals is None:            # quality run: one question at a time, never queued
        for i, q in enumerate(questions):
            while relay.limiter.expected_wait(3500) > 0:
                time.sleep(0.5)
            one(i, q, time.time())
    else:
        start = time.time() + 1
        threads = []
        for i, offset in enumerate(sorted(arrivals)):
            arrival = start + offset
            time.sleep(max(0, arrival - time.time()))
            t = threading.Thread(target=one, args=(i, questions[i % len(questions)], arrival))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    if server:
        print(f"stand-in peak requests in flight: {si.peak_in_flight}")
        server.shutdown()

    os.makedirs("results", exist_ok=True)
    rows.sort(key=lambda r: r["request_id"])
    with open(f"results/{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    with open(f"results/{name}_detail.jsonl", "w", encoding="utf-8") as f:
        for d in sorted(details, key=lambda d: d["request_id"]):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    if relay.replay:                # keep real answers so the stand-in can replay them
        path = "data/replay.json"
        old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        old.update(relay.replay)
        json.dump(old, open(path, "w", encoding="utf-8"), ensure_ascii=False)

    if source == "real":
        used = sum(r["tokens_in"] + r["tokens_out"] for r in rows)
        config.spend_daily_tokens(used, limits["tpd"])
        print(f"this run used {used} tokens; daily budget left now: {config.daily_tokens_left(limits['tpd']):.0f}")
    by_status = {s: sum(r["status"] == s for r in rows) for s in ("ok", "rate_limited", "timeout", "error")}
    print(f"done: {len(rows)} requests {by_status} -> results/{name}.csv")


if __name__ == "__main__":
    main()
