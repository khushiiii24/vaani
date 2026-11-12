# The Relay: write-up

## 1. What I built and why

`pipeline.py` runs five steps: **admit** (check the daily budget, book the request's token cost),
**understand** (an optional model call: which paper, what to search for), **retrieve** (the tool, BM25,
searching again wider if the paper filter finds nothing: the loop), **decide** (off-topic questions are
refused without a model call: the branch) and **draft + check**. The model is `gpt-oss-20b` on Groq's free tier.

**The decision that shaped it most: nothing reaches the person unchecked, and nobody gets nothing.**
A streamed piece is shown only if it cites a passage we retrieved and every number in it appears there.
If the model can't be used in time, the person gets the two most relevant passages, quoted and labelled.
Speed costs completeness, never correctness: **0 wrong in 555 graded replies**. **"Fast"** means a first
token within 10 s, because after that the person has left. At normal load I aim for p50 ≤ 1.5 s and p95 ≤ 3 s.

## 2. The real service and my stand-in

`probe_groq.py` read the headers and hit each limit on purpose (Table 1). **Groq's limits are buckets
that refill continuously.** The RPM 429 said "try again in 2s" (60 s / 30), the TPM 429 said 14.3 s
(missing tokens / 133 per s), and daily requests come back one every 86.4 s. My first limiter used 60 s
windows and answered 2 of a real 10-request burst; the bucket version answers 3, the most the free tier allows.
**The stand-in** copies Groq's API, buckets, 429 text and `retry-after`. Its first-token time is fitted to 70
real calls (p50 556 ms, p95 977 ms), and it replays Groq's recorded answers, so they can be graded. It adds
errors, slow successes, slow-down under load and 10× limits. **Least like Groq:** the slow-down under
load is invented, because the free tier can't be loaded enough to measure it.

**Table 1: Limits found in the real service (openai/gpt-oss-20b)**

| Limit | Value found | How found | Confidence |
|---|---|---|---|
| Requests per minute | 30 | request 32 of a back-to-back run got the first 429 ("Limit 30, Used 30"; one extra slipped in as the limit refilled) | high |
| Tokens per minute | 8000 | x-ratelimit-limit-tokens header; 1 of 5 big simultaneous requests got 429 | high |
| Requests per day | 1000 | x-ratelimit-limit-requests header | high |
| Tokens per day | 200000 | not in any header, not reached; Groq docs | low |
| Requests in flight at once | at least 10 | 10 of 10 simultaneous requests succeeded; not pushed higher | low |
| What it returns when a limit is hit | HTTP 429, retry-after in whole seconds, and a body naming the limit: "Rate limit reached … on tokens per minute (TPM): Limit 8000, Used 7926, Requested 1983. Please try again in 14.3175s" | data/probe_429.json | high |
| Time until it recovers | RPM: retry-after 2.0 s, worked after 2.5 s. TPM: retry-after 15.0 s, worked after 14.7 s | waited retry-after minus 1 s, then tried every 0.5 s | high |

## 3. Results

On 20 questions I wrote (16 answerable, including table and formula facts; 4 the papers don't answer), the
best setting got 16/16 correct and cited and refused 4/4. Answers cost 1,683–2,805 tokens. Percentiles
are nearest-rank. **The number nobody could guess:** the free tier answers only **3 of 10 simultaneous
questions** (8,000 tokens/min against about 2,200 per answer). The daily budget is **about 95 answers,
coming back at about 4 per hour**.

**Table 2a: Latency per load scenario** (`real_burst_v1` = the same burst with my first limiter)

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
| real_steady | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 2185 | $0 ($0.190 at list price) |
| real_burst_v1 | 20.0 | 80.0 | 0.0 | 0.00 | 8 | 602 | $0 ($0.057 at list price) |
| real_burst | 30.0 | 70.0 | 0.0 | 0.00 | 4 | 664 | $0 ($0.057 at list price) |
| steady | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 2004 | $0 ($0.175 at list price) |
| burst | 68.6 | 30.0 | 1.4 | 0.00 | 36 | 1337 | $0 ($0.115 at list price) |
| overload | 90.6 | 8.9 | 0.6 | 0.01 | 8 | 1727 | $0 ($0.146 at list price) |
| rpm_halved | 88.9 | 8.9 | 2.2 | 0.00 | 4 | 1693 | $0 ($0.144 at list price) |
| daily_exhausted | 70.0 | 30.0 | 0.0 | 0.00 | 1 | 1300 | $0 ($0.112 at list price) |
| burst_no_limiter | 73.3 | 23.3 | 3.3 | 0.52 | 50 | 1392 | $0 ($0.120 at list price) |
| burst_naive_retry | 70.0 | 26.7 | 3.3 | 1.07 | 50 | 1334 | $0 ($0.115 at list price) |

## 4. Where the time goes

The slowest real answer (`quality_S3-0015`, 3,238 ms) spent 0 ms waiting, 0 ms retrying and 3,238 ms
working. Inside the drafting call, Groq sent nothing for 2,745 ms, thought for 51 ms, then answered. So the
delay was **before the first token, inside Groq**. A per-request clock splits time into waiting, working and
retrying, and the stream reader stamps the first reasoning byte and the first answer byte.

## 5. When a limit is hit

**Per minute:** a request books its whole cost at the door. If room comes within 7.5 s (counting the line),
it waits, first come first served. If not, it gets passages at once: weaker, but not dropped. While there is
a line, newcomers skip the optional step. Each fix followed a measurement. Booking per call let 5 of 10 real
burst requests pay for a call and then be turned away. In the stand-in, not counting the line made them wait
6.7 s for passages. Now they get passages in a median **2 ms**, the longest wait is 7.2 s, and there were **0 per-minute 429s**.
**Per day:** below 20% left, lean mode (no understand step). At zero, everyone gets passages and is told
when answers resume. No user is favoured.

## Retrieval: quality versus cost

**Table 3: Retrieval settings** (real Groq)

| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| S1: 120 words, 6 pieces, re-rank no | 20 | 80.0 | 0.0 | 20.0 | 1683 | 1085 / 1833 | 1085 / 1833 |
| S2: 250 words, 4 pieces, re-rank no | 20 | 75.0 | 0.0 | 20.0 | 2065 | 1056 / 1572 | 1056 / 1572 |
| S3: 250 words, 4 pieces, re-rank yes | 20 | 70.0 | 0.0 | 20.0 | 2066 | 1101 / 1681 | 1106 / 1681 |
| S4: 500 words, 3 pieces, re-rank no | 20 | 80.0 | 0.0 | 20.0 | 2805 | 1611 / 2530 | 1611 / 2530 |

The rest of each row refused an answerable question (a miss, not a wrong answer): S2: 5.0%, S3: 10.0%.

**Chosen: S1.** It was as accurate as S4, with 40% fewer tokens and a first token 0.5 s sooner (the load runs
used S2, the default before this test). Re-ranking hurt: it promoted the abstract, which repeats "English
constituency parsing", over page 10 (beam 21), so the model refused rather than guessed. Near the limit,
retrieval drops the understand call.

## How this library will mislead a naive retriever

1. **Shared vocabulary:** "warmup" is in all three papers. For *"What warmup_steps did the Transformer use?"*, the right page scores 17.1 against Moshi's 16.3, and with 500-word pieces Moshi wins.
2. **Unicode math:** H-Net's `𝑝𝑡≥0.5` is invisible to keyword search, so *"Above what boundary probability…?"* fails. 250-word pieces miss it even after normalising.
3. **Two numbers, one question:** for *"What beam size did they use?"*, it is 4 for translation and 21 for parsing. H-Net's DNA gain is "nearly 4×" in its abstract and "3.6×" in its introduction.

## 6. Retries

On a 429, honour `retry-after` at most twice, and only if it fits before the deadline. On an error or a
silent call, retry once, and only before the first token, since a later retry could repeat shown text.
**Made worse** (stand-in burst, limiter off): 16 requests got a 429 and retried 31 times. Only 2 were saved,
and 14 got passages after 6.2 s instead of at once. Immediate retries made 64 retries and saved 0; with the
limiter there were 0 retries. I'd retry a 429 at most once.

## 7. What the person sees

At 3 pm with the budget gone, the reply arrives in about 2 ms (stand-in run, shortened): *"I can't write a
full answer right now (the daily model budget is used up; written answers resume in about 39 minutes).
These passages from the papers look most related…"*, followed by two quoted passages with page and section. The wait comes from the refill rate; on the real 200k/day tier it
would say about 15 minutes.

## 8. What I'm unsure about, what I left out, what I learned

**Least certain:** that tokens per day refill like requests per day. No header shows it. If they reset at
midnight, lean mode starts at the wrong time, though Groq's per-day 429 still triggers the fallback.
**Not built:** a second model call to check answers (it would halve throughput), and an answer cache
(good for bursts, but it needs rules for stale answers). **New to me:** I thought a rate limit was a counter
that resets every minute or every night. Groq showed me it is a bucket that slowly fills back up. The 429
said try again in 2 seconds, not 60. That is why my first limiter was stricter than Groq itself.

## Your feedback on Vaani

**What stood out:** you can hear a real call before signing up, and the Hindi and Hinglish examples make it
clear who it is for. Teacher AI shows the prompt change before saving it, which is the same idea I used
here. **What confused me:** latency is under 500 ms in one place and about 450 ms p90 in another, but the
page never says what is timed or under how much load. **One change:** show the free plan limits and how
latency is measured right next to the Start free button.

## How I worked with the assistant

I used Claude for most of the code and the first draft of this write-up. The full chat is attached as it
happened. **Three prompts that mattered:**
1. My first prompt asked only for the idea and the steps, nothing beyond the brief, and no building yet.
2. When it started, I asked for simple and clear work, which kept the code small and easy to explain.
3. Pasting my probe output with *"are all steps correct? now what to do next"*. My real numbers showed the limits refill slowly, and the limiter was rebuilt around that.

**What it got wrong that I caught:** it assumed `llama-3.1-8b-instant` was available. My probe failed on
every request with "model does not exist", and we switched to gpt-oss-20b. **Where I overruled it:** it
planned to run S1, S3 and S4 the next day. I asked why I had to wait, it worked out the budget again, and I
ran S1 and S3 that night. **What is mine:** every real measurement ran on my own Groq account from my
machine, and I chose when to run each one within the daily budget. I sent every output back so we could check it
before moving on. The assistant drafted the 20 test questions. I read
them and kept them.
