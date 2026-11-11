"""
Calibrate the stand-in's timing from every real Groq call we have, not just the probe's 10:
  - probe step B (main model)                      data/probe_real.csv
  - the drafting call of every real pipeline run   results/real_*_detail.jsonl, results/quality_*_detail.jsonl

Writes ttft_p50_ms / ttft_p95_ms (first answer token of one model call) into data/calibration.json.
The stand-in draws its first-token times from a log-normal with that median and p95.

    python calibrate_standin.py
"""
import csv
import glob
import json
import math

with open("data/calibration.json", encoding="utf-8") as f:
    cal = json.load(f)

ttft = [float(r["ttft_ms"]) for r in csv.DictReader(open("data/probe_real.csv", encoding="utf-8"))
        if r["test"] == "B_latency" and r["model"] == cal["model"] and r["status"] == "ok"]
n_probe = len(ttft)
for path in glob.glob("results/real_*_detail.jsonl") + glob.glob("results/quality_*_detail.jsonl"):
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d["model_ttft_ms"] != "":
            ttft.append(float(d["model_ttft_ms"]))
ttft.sort()
thinking = sorted(json.loads(line)["thinking_ms"] for path in glob.glob("results/quality_*_detail.jsonl")
                  for line in open(path, encoding="utf-8") if json.loads(line).get("thinking_ms"))


def pct(p):
    return ttft[max(0, math.ceil(p / 100 * len(ttft)) - 1)]


cal.setdefault("probe_ttft_p50_ms", cal.get("ttft_p50_ms"))
cal.setdefault("probe_ttft_p95_ms", cal.get("ttft_p95_ms"))
cal.update({"ttft_p50_ms": pct(50), "ttft_p95_ms": pct(95), "ttft_p99_ms": pct(99), "ttft_max_ms": ttft[-1],
            "ttft_samples": len(ttft), "ttft_samples_from_probe": n_probe,
            "thinking_ms": thinking[len(thinking) // 2] if thinking else 70})
with open("data/calibration.json", "w", encoding="utf-8") as f:
    json.dump(cal, f, indent=2)
print(f"{len(ttft)} real model calls ({n_probe} from the probe): first answer token "
      f"p50 {pct(50):.0f} ms, p95 {pct(95):.0f} ms, p99 {pct(99):.0f} ms, max {ttft[-1]:.0f} ms; "
      f"thinking median {cal['thinking_ms']} ms over {len(thinking)} answers")
