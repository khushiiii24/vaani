## Table 1: Limits found in the real service (openai/gpt-oss-20b)

| Limit | Value found | How found | Confidence |
|---|---|---|---|
| Requests per minute | 30 | request 32 of a back-to-back run got the first 429 ("Limit 30, Used 30"; one extra slipped in as the limit refilled) | high |
| Tokens per minute | 8000 | x-ratelimit-limit-tokens header; 1 of 5 big simultaneous requests got 429 | high |
| Requests per day | 1000 | x-ratelimit-limit-requests header | high |
| Tokens per day | 200000 | not in any header, not reached; Groq docs | low |
| Requests in flight at once | at least 10 | 10 of 10 simultaneous requests succeeded; not pushed higher | low |
| What it returns when a limit is hit | HTTP 429, retry-after in whole seconds, and a body naming the limit: "Rate limit reached … on tokens per minute (TPM): Limit 8000, Used 7926, Requested 1983. Please try again in 14.3175s" | data/probe_429.json | high |
| Time until it recovers | RPM: retry-after 2.0 s, worked after 2.5 s. TPM: retry-after 15.0 s, worked after 14.7 s | waited retry-after minus 1 s, then tried every 0.5 s | high |

**How the limits refill (measured):** not in fixed windows. The RPM 429 said "try again in 2s" (60 s / 30 requests); the TPM 429 asked to wait (tokens missing) / (8000 / 60 per s). Daily requests refill one at a time too: x-ratelimit-reset-requests was 1h19m12s after 55 requests, i.e. 86.4 s each. Tokens per day are assumed to refill the same way (not visible).

Also measured: first answer token p50/p95 = 617/1841 ms over 10 probe requests (556.0/977.0 ms over all 70 real calls); max_tokens booked against TPM up front: False (tokens left dropped by 108 for max_tokens=3000; the TPM 429 for a ~1,900-token prompt with max_tokens 700 said "Requested 1983"). Probe used 65 requests, 32100 tokens.

## Table 2a: Latency per load scenario

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

## Table 2b: Outcomes, same rows

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

## Table 3: Retrieval settings

| Setting (piece size, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| S1: 120 words, 6 pieces, re-rank no | 20 | 80.0 | 0.0 | 20.0 | 1683 | 1085 / 1833 | 1085 / 1833 |
| S2: 250 words, 4 pieces, re-rank no | 20 | 75.0 | 0.0 | 20.0 | 2065 | 1056 / 1572 | 1056 / 1572 |
| S3: 250 words, 4 pieces, re-rank yes | 20 | 70.0 | 0.0 | 20.0 | 2066 | 1101 / 1681 | 1106 / 1681 |
| S4: 500 words, 3 pieces, re-rank no | 20 | 80.0 | 0.0 | 20.0 | 2805 | 1611 / 2530 | 1611 / 2530 |

The rest of each row refused an answerable question (a miss, not a wrong answer): S2: 5.0%, S3: 10.0%.

## Answer quality inside the load runs (same grader)

| Scenario | correct % | wrong % | refused correctly % | refused wrongly % | not graded % |
|---|---|---|---|---|---|
| real_steady | 100.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| real_burst_v1 | 50.0 | 0.0 | 0.0 | 50.0 | 0.0 |
| real_burst | 70.0 | 0.0 | 0.0 | 30.0 | 0.0 |
| steady | 83.3 | 0.0 | 13.3 | 3.3 | 0.0 |
| burst | 57.1 | 0.0 | 14.3 | 18.6 | 10.0 |
| overload | 57.2 | 0.0 | 17.2 | 6.1 | 19.4 |
| rpm_halved | 61.1 | 0.0 | 14.4 | 6.7 | 17.8 |
| daily_exhausted | 66.7 | 0.0 | 3.3 | 16.7 | 13.3 |
| burst_no_limiter | 55.0 | 0.0 | 16.7 | 16.7 | 11.7 |
| burst_naive_retry | 55.0 | 0.0 | 16.7 | 18.3 | 10.0 |

not graded = the person gave up before a reply, or the stand-in had no recorded real answer for that exact prompt and invented placeholder text (not a real answer, so not graded).

## Slowest requests (from results/*_detail.jsonl)

| request | status | mode | first token ms | total ms | waiting ms | working ms | retrying ms | model first token ms | retries |
|---|---|---|---|---|---|---|---|---|---|
| overload-0111 | timeout | passages_only | 9710 | 9710 | 4360 | 1 | 5349 |  | 0 |
| rpm_halved-0082 | timeout | passages_only | 9709 | 9709 | 4644 | 1 | 5064 |  | 0 |
| rpm_halved-0083 | timeout | passages_only | 9708 | 9708 | 5719 | 1 | 3988 |  | 0 |
| burst_naive_retry-0036 | timeout | passages_only | 9097 | 9097 | 0 | 3088 | 6009 |  | 0 |
| burst_no_limiter-0036 | timeout | passages_only | 9097 | 9097 | 0 | 3088 | 6009 |  | 0 |
| burst_no_limiter-0035 | timeout | passages_only | 9049 | 9049 | 1 | 3039 | 6010 |  | 0 |
| burst_naive_retry-0035 | timeout | passages_only | 9048 | 9048 | 0 | 3039 | 6009 |  | 0 |
| overload-0113 | ok | lean | 9019 | 9023 | 5355 | 3668 | 0 | 3563 | 0 |
