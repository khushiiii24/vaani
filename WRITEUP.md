# The Relay: write-up

## 1. What I built and why

`pipeline.py` runs five steps:
1. **Admit:** check the daily budget and book the request's token cost.
2. **Understand:** an optional model call names the paper and search words.
3. **Retrieve:** the tool, BM25 over the papers, searching again wider if the paper filter finds nothing (the loop).
4. **Decide:** off-topic questions are refused without a model call (the branch).
5. **Draft + check.**

The model is `gpt-oss-20b` on Groq's free tier; the Llama models returned 404 for my key.
**The decision that shaped it most: nothing reaches the person unchecked, and nobody gets nothing.** Each
streamed piece must cite a passage we retrieved, and every number in it must appear there, or it isn't shown.
If the model can't be used in time, the person gets the two most relevant passages, quoted and labelled.
Speed costs completeness, never correctness: **0 wrong in 535 graded replies.** **"Fast"** means the first
token within 10 s (otherwise the person has left), with a target of p50 ≤ 1.5 s and p95 ≤ 3 s at normal load.

## 2. The real service and my stand-in

`probe_groq.py` (77 requests) read the headers and hit each limit on purpose (Table 1). **Groq's limits
are buckets that refill continuously.** The RPM 429 said "try again in 2s" (60 s / 30). The TPM 429 said
"14.3s", which is the missing tokens / (8000/60). Daily requests come back one every 86.4 s. My first
limiter used 60-second windows: it answered 2 of a real 10-request burst. The bucket version answers 3, the
most 8,000 tokens/min allows.

**The stand-in** copies the API, the buckets, the 429 text and `retry-after`. Its first-token time is
log-normal, fitted to 70 real calls (p50 556 ms, p95 977 ms). It replays Groq's recorded answer for the same
prompt, so its answers can be graded. It adds errors (2%), slow successes (3%), slow-down under load, and 10× limits.
**Least like Groq:** the slow-down under load is invented, since the free tier can't be loaded enough to measure it.

**Table 1: Limits found in the real service (openai/gpt-oss-20b)**

| Limit | Value found | How found | Confidence |
|---|---|---|---|
| Requests per minute | 30 | tiny requests back to back: request 32 got the first 429, whose body says "Limit 30, Used 30" (one extra got through because the limit refills during the burst) | high |
| Tokens per minute | 8000 | x-ratelimit-limit-tokens header on every reply; 1 of 5 big simultaneous requests got 429 | high |
| Requests per day | 1000 | x-ratelimit-limit-requests header | high |
| Tokens per day | 200000 | not in any header, not reached; from Groq's limits docs | low |
| Requests in flight at once | at least 10 | 10 of 10 simultaneous requests succeeded; did not push higher (would cost budget for little) | low |
| What it returns when a limit is hit | HTTP 429, a retry-after header (whole seconds) and a body naming the limit: "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01m73aw1ncedqayzy0hdm8cn6m` service tier `on_demand` on tokens per minute (TPM)" | captured in data/probe_429.json | high |
| Time until it recovers | RPM: retry-after 2.0 s, worked again after 2.5 s. TPM: retry-after 15.0 s, worked again after 14.7 s | waited retry-after minus 1 s, then retried every 0.5 s | high |

## 3. Results

Quality on 20 questions I wrote (16 answerable, including table and formula facts; 4 the papers don't answer):
the best setting got 16/16 correct and cited, and refused all 4 correctly. Answers cost 1,683–2,185 tokens.
Percentiles are nearest-rank over each row.
**The number nobody could guess:** the free tier answers only **3 of 10 simultaneous questions**
(8,000 tokens/min against about 2,200 per answer), and the daily budget is **about 95 answers, coming back at about 4 per hour**.

**Table 2a: Latency per load scenario**

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| real_steady | real | 10 | 271 | 2.2 | 1275 / 1555 / 1555 | 1275 / 1555 / 1555 |
| real_burst_v1 | real | 10 | 1 | 510.2 | 438 / 1033 / 1033 | 438 / 1038 / 1038 |
| real_burst | real | 10 | 1 | 455.9 | 2 / 1189 / 1189 | 2 / 1189 / 1189 |
| steady | stand-in | 30 | 176 | 10.2 | 1319 / 1668 / 1955 | 1319 / 1668 / 1955 |
| burst | stand-in | 70 | 109 | 38.4 | 1893 / 5729 / 8129 | 1893 / 5729 / 8129 |
| overload | stand-in | 180 | 186 | 58.1 | 1441 / 7557 / 9019 | 1441 / 7561 / 9023 |
| rpm_halved | stand-in | 90 | 184 | 29.4 | 1456 / 7396 / 9709 | 1456 / 7396 / 9709 |
| daily_exhausted | stand-in | 30 | 174 | 10.3 | 1105 / 1670 / 1957 | 1109 / 1670 / 1957 |
| burst_no_limiter | stand-in | 60 | 11 | 330.4 | 5722 / 8520 / 9097 | 5722 / 8547 / 9097 |
| burst_naive_retry | stand-in | 60 | 11 | 330.4 | 3052 / 8586 / 9097 | 3052 / 8586 / 9097 |

**Table 2b: Outcomes, same rows**

| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
|---|---|---|---|---|---|---|---|
| real_steady | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 2185 | $0 on free tier ($0.190 at list price) |
| real_burst_v1 | 20.0 | 80.0 | 0.0 | 0.00 | 8 | 602 | $0 on free tier ($0.057 at list price) |
| real_burst | 30.0 | 70.0 | 0.0 | 0.00 | 4 | 664 | $0 on free tier ($0.057 at list price) |
| steady | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 2004 | $0 on free tier ($0.175 at list price) |
| burst | 68.6 | 30.0 | 1.4 | 0.00 | 36 | 1337 | $0 on free tier ($0.115 at list price) |
| overload | 90.6 | 8.9 | 0.6 | 0.01 | 8 | 1727 | $0 on free tier ($0.146 at list price) |
| rpm_halved | 88.9 | 8.9 | 2.2 | 0.00 | 4 | 1693 | $0 on free tier ($0.144 at list price) |
| daily_exhausted | 70.0 | 30.0 | 0.0 | 0.00 | 1 | 1300 | $0 on free tier ($0.112 at list price) |
| burst_no_limiter | 73.3 | 23.3 | 3.3 | 0.52 | 50 | 1392 | $0 on free tier ($0.120 at list price) |
| burst_naive_retry | 70.0 | 26.7 | 3.3 | 1.07 | 50 | 1334 | $0 on free tier ($0.115 at list price) |

## 4. Where the time goes

**Slowest real answer** (`quality_S3-0015`, 3,238 ms):

| Part | Time |
|---|---|
| Waiting in our queue | 0 ms |
| Retrying | 0 ms |
| Working | 3,238 ms |
| Inside working: Groq silent before any token | 2,745 ms |
| Inside working: thinking | 51 ms |

So the delay came **before the first token, inside Groq**. I know because a per-request clock sorts time
into waiting, working and retrying, and the stream reader stamps the first reasoning and answer bytes.
Under stand-in overload the delay moves into *our* line, on purpose: `overload-0137` waited 7,058 ms
because the line predicted it would fit, and it did.

## 5. When a limit is hit

**Per minute:** a request books its whole cost at the door. If room will come within 7.5 s (counting the
line), it waits, first come first served. Otherwise it gets passages **at once**: weaker, but not dropped.
While there is a line, newcomers skip the optional step. I got here by measuring:
1. Booking per call let 5 of 10 real burst requests pay for a call and then be turned away.
2. Not counting the line made the stand-in's turned-away requests wait 6.7 s.
3. Now: a median **2 ms**, the longest wait 7.2 s, and **0 per-minute 429s**.

**Per day:** below 20% left, lean mode (no understand step). At zero, everyone gets passages and is
told when answers resume. No user is favoured; the brief gives no tiers.

## Retrieval: quality versus cost

**Table 3: Retrieval settings** (real Groq; S4 = 500 words × 3 runs on day 2)

| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| S1: 120 words, 6 pieces, re-rank no | 20 | 80.0 | 0.0 | 20.0 | 1683 | 1085 / 1833 | 1085 / 1833 |
| S2: 250 words, 4 pieces, re-rank no | 20 | 75.0 | 0.0 | 20.0 | 2065 | 1056 / 1572 | 1056 / 1572 |
| S3: 250 words, 4 pieces, re-rank yes | 20 | 70.0 | 0.0 | 20.0 | 2066 | 1101 / 1681 | 1106 / 1681 |

The rest of each row refused an answerable question (a miss, not a wrong answer): S2: 5.0%, S3: 10.0%.

**Chosen: S1.** It was the most accurate and 18% cheaper than S2. The load runs used S2, the default before this comparison. Re-ranking hurt: it promoted the abstract,
which repeats "English constituency parsing", over page 10 (beam 21), so the model **refused rather than guessing**.
Near the limit, retrieval drops the understand call.

## How this library will mislead a naive retriever

1. **Shared vocabulary.** "Warmup" appears in all three papers. For *"What warmup_steps did the Transformer use?"*, the right page scores 17.1 against Moshi's 16.3, and with 500-word pieces Moshi wins.
2. **Unicode math.** H-Net's `𝑝𝑡≥0.5` is invisible to keyword search, so *"Above what boundary probability…?"* fails. With 250-word pieces it fails even after normalisation.
3. **Two numbers, one question.** Beam size is 4 for translation but 21 for parsing; H-Net's DNA gain is "nearly 4×" in the abstract and "3.6×" in the introduction.

Also, the retrieval score can't tell unanswerable questions apart. The English–Romanian question outscores 13 of 16
answerable ones, so the model must do the refusing.

## 6. Retries

**Policy:**
- **429:** honour `retry-after`, at most twice, and only if it fits before the deadline.
- **Errors and silent calls:** retry once, only before the first token, since a later retry could repeat text already shown.

**Made worse** (stand-in burst, limiter off): 16 requests got a 429 and retried 31 times. Only 2 were saved;
14 got passages after 6.2 s instead of at once. Naive immediate retries: 64 retries, 0 saved. With the limiter: 0 retries.
I'd retry a 429 at most once.

## 7. What the person sees

At 3 pm the daily budget is gone. Within about 2 ms the person gets this (from the stand-in run, passages shortened with …):

> I can't write a full answer right now (the daily model budget is used up; written answers resume in
> about 39 minutes). These passages from the papers look most related. They may not answer your
> question: – "transformer with dmodel = 1024on the Wall Street Journal (WSJ) portion of the Penn
> Treebank [25] …" (Attention Is All You Need, p.9, 6.3 English Constituency Parsing) – "Pdrop = 0.1 …
> For the big models, we averaged the last 20 checkpoints. We used beam search with a beam size of 4
> and length penalty ..." (Attention Is All You Need, p.8, 6.1 Machine Translation)

The wait is computed from the refill rate. On the real 200k-per-day tier it would say about 15 minutes.

## 8. What I'm unsure about, what I left out, what I learned

**Least certain:** that *tokens* per day refill like requests per day do; no header shows it. If they reset
at midnight instead, lean mode starts at the wrong time, though Groq's per-day 429 still triggers the fallback.
**Not built:**
1. A second model call to verify answers, which would halve throughput.
2. An answer cache, which would help bursts but needs rules for staleness.

**New to me:** **[YOU]**

## Your feedback on Vaani

**[YOU: 3–5 honest lines after visiting https://www.vaani.dev/]**

## How I worked with the assistant

**[YOU: the chat export, plus half a page: three prompts that mattered, one thing it got wrong that you caught,
one decision you overruled, what is distinctly yours]**
