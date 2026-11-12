# The Relay: a cited-answer pipeline under load

A small Python pipeline that answers questions about three arXiv papers with
citations, plus a stand-in model service and a load runner that measure how it
behaves when traffic and rate limits get rough.

## Files (in the order a request uses them)

| File | What it does |
|---|---|
| `fetch_papers.py` | Downloads the 3 papers at the exact versions (v7, v1, v2), saves page text to `data/pages.json` |
| `config.py` | **Every number I chose**: patience, timeouts, retries, limits, retrieval settings |
| `retriever.py` | The tool: splits papers into pieces (page + section kept for citations), BM25 search, optional re-rank |
| `llm_client.py` | One streaming call to an OpenAI-style API (Groq or the stand-in); clear errors for 429 / 5xx / silence |
| `pipeline.py` | The 5 steps, the rate limiter, the daily budget, the retry policy, the answer check, the fallback |
| `standin_server.py` | Fake Groq: slow, fails, 429s at limits, slower under load, streams; calibrated from the probe |
| `probe_groq.py` | Measures the real Groq service (latency spread, limits, what a 429 looks like, recovery) |
| `run_load.py` | Runs one load shape, grades every answer, writes `results/<scenario>.csv` |
| `calibrate_standin.py` | Fits the stand-in's timing to every real call (probe + pipeline runs) |
| `make_tables.py` | Joins everything into `results.csv` and computes every table from it into `tables.md` |
| `questions.json` | 20 test questions: 16 answerable (with the facts a correct answer must contain), 4 the papers don't answer |

## The pipeline (one request)

```
 question
   |
 1 ADMIT        daily budget left?  no  -> passages-only reply (no model call)
   |            low or per-minute limit close -> "lean" mode (skip step 2)
 2 UNDERSTAND   small model call -> {on_topic, which paper, search words}   (optional, 3 s max)
   |
 3 RETRIEVE     tool: BM25 search over the papers
   |            LOOP: nothing relevant with the paper filter -> search again without it
 4 DECIDE       BRANCH: off-topic and nothing relevant -> "the papers don't answer this"
   |
 5 DRAFT+CHECK  model writes the answer with [P1] citations, streamed;
                every piece is checked BEFORE the person sees it:
                  cites a passage we retrieved?  every number appears in that passage?
                a piece that fails stops the answer there
```

If the model can't be used in time (rate limit, daily budget gone, errors), the
person still gets a reply right away: the two most relevant passages, quoted with
citations, clearly marked as "not a written answer".

## How to run (Windows, from this folder)

Unzip into `C:\Users\khush\OneDrive\Desktop\assign`, next to the `papers` and `data`
folders that `fetch_papers.py` already made.

```
cd C:\Users\khush\OneDrive\Desktop\assign
pip install requests pypdf
python fetch_papers.py                       # already done
```

Set your Groq key **in the terminal only**. Never put it in a file you submit.
```
$env:GROQ_API_KEY="gsk_..."                  # PowerShell
set GROQ_API_KEY=gsk_...                     # Command Prompt
```

The model is `openai/gpt-oss-20b` (the Llama models answered 404 on this account).
Its free-tier budget is **200k tokens per day**, and it refills continuously (about 8,300 tokens
per hour), not at midnight. Every real run prints how much is left (`data/daily_usage.json`).

**1. Measure the real service** (about 15 min, about 32k tokens)
```
python probe_groq.py
```

**2. Real runs** (they also record Groq's answers in `data/replay.json` for the stand-in)
```
python run_load.py real_steady               # 10 requests, 2/min
python run_load.py real_burst                # 10 requests at once
python run_load.py quality --setting S1      # 20 questions each, about 34-60k tokens each
python run_load.py quality --setting S2
python run_load.py quality --setting S3
python run_load.py quality --setting S4      # biggest: run when about 60k tokens are left
```

**3. Calibrate the stand-in from every real call, then the stand-in runs** (no key, about 3 min each)
```
python calibrate_standin.py
python run_load.py steady
python run_load.py burst
python run_load.py overload
python run_load.py rpm_halved
python run_load.py daily_exhausted
python run_load.py burst_no_limiter
python run_load.py burst_naive_retry
```

**4. Tables**
```
python make_tables.py                        # writes results.csv and tables.md
```

## Raw results kept

- `results.csv`: one row per request, exactly the columns the brief asks for
- `results/<scenario>_detail.jsonl`: answer text, mode, and waiting / working / retrying time per request
- `data/probe_real.csv`, `data/probe_429.json`, `data/calibration.json`: the real-service probe
- `data/attempt1_llama404_*`: the first probe, when both Llama models answered 404
- `results/history/`: earlier versions' runs that the write-up quotes (not in results.csv)
