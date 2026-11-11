"""
The Relay pipeline. One request goes through these steps:

  1. ADMIT       check the daily budget -> full / lean / passages-only mode
  2. UNDERSTAND  small model call: is it on topic, which paper, what to search for
                 (skipped when the limit is close: it is the cheapest thing to drop)
  3. RETRIEVE    the tool: BM25 search over the papers
                 LOOP: if nothing relevant was found, search once more, wider
  4. DECIDE      BRANCH: still nothing relevant -> say "the papers don't say", no model call
  5. DRAFT+CHECK model writes the answer with [P1]-style citations, streamed.
                 Each piece is checked BEFORE the person sees it:
                   - it must end with a citation to a passage we actually retrieved
                   - every number in it must appear in the cited passage
                 A piece that fails stops the answer there.

If the model can't be reached in time (rate limit, daily budget gone, errors),
the person still gets a reply: the most relevant passages, quoted with
citations, clearly labelled as "not a written answer".
"""
import json
import random
import re
import threading
import time
from collections import deque

import config
from llm_client import CallTimeout, RateLimited, ServerError, stream_chat
from retriever import Retriever

REFUSAL = "The three papers in my library don't answer this."
FALLBACK = "I can't write a full answer right now"

UNDERSTAND_SYSTEM = (
    "You route questions about three research papers: "
    "'attention' = Attention Is All You Need (the Transformer); "
    "'moshi' = Moshi, a speech-text model for real-time spoken dialogue (uses the Mimi audio codec); "
    "'hnet' = Dynamic Chunking for End-to-End Hierarchical Sequence Modeling (H-Net). "
    'Reply with JSON only: {"on_topic": true or false, "paper": "attention" or "moshi" or "hnet" or "any", '
    '"search": "keywords to search the paper for"}. '
    "on_topic is false only if the question has nothing to do with these papers or their topics."
)

DRAFT_SYSTEM = (
    "You answer questions using ONLY the numbered passages below, taken from research papers.\n"
    "Rules:\n"
    "1. End every sentence with the passage id it comes from, like [P2]. Use [P1][P3] for two.\n"
    "2. Copy numbers exactly as the passage writes them.\n"
    "3. If the passages do not contain the answer, reply with exactly: NOT_IN_PAPERS\n"
    "4. Do not use outside knowledge. At most 4 sentences."
)

CITE_RE = re.compile(r"\[P\d+(?:\]\s*\[P\d+)*\]")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


class StopStream(Exception):
    pass


# ====================================================================== limiter
class Limiter:
    """Client-side copy of the provider's per-minute limits, shared by all requests.
    We wait here instead of sending a request we know will get a 429.

    Groq refills its limits continuously. Measured: a requests-per-minute 429 said "try again
    in 2s" (= 60 s / 30), a tokens-per-minute 429 said 14.3 s (= missing tokens / (8000/60 per s)).
    So this is a token bucket: it holds up to one minute's worth and refills at limit/60 per second.
    (A first version used fixed 60 s windows and booked per call: in the real 10-request burst it
    served 2 and let 5 others pay for a model call before turning them away; this version serves 3,
    the most 8,000 tokens/min allows, and turns the rest away before they spend anything.)"""

    def __init__(self, rpm, tpm, enabled=True):
        self.rpm, self.tpm, self.enabled = rpm, tpm, enabled
        self.lock = threading.Lock()
        self.req_left = rpm * config.SAFETY      # requests we may still send right now
        self.tok_left = tpm * config.SAFETY      # tokens we may still use right now
        self.last = time.time()
        self.blocked_until = 0.0                 # set when the provider says "retry after"
        self.queued = 0.0                        # tokens wanted by requests already waiting in line
        self.line = deque()                      # those requests, in arrival order

    def _refill(self, now):
        dt, self.last = now - self.last, now
        self.req_left = min(self.rpm * config.SAFETY, self.req_left + dt * self.rpm / 60)
        self.tok_left = min(self.tpm * config.SAFETY, self.tok_left + dt * self.tpm / 60)

    def _wait_time(self, tokens, requests, now):
        self._refill(now)
        if now < self.blocked_until:
            return self.blocked_until - now
        return max(0.0, (requests - self.req_left) / (self.rpm / 60),
                   (tokens - self.tok_left) / (self.tpm / 60))

    def acquire(self, tokens, deadline, requests=1):
        """Wait for room under the limits. False if the room comes after the deadline.
        Decided at the door, counting everyone already waiting: in the stand-in burst, people
        who could never make it waited a median 6.7 s before getting the passages-only reply."""
        if not self.enabled:
            return True
        with self.lock:
            now = time.time()
            wait = self._wait_time(tokens + self.queued, requests, now)
            if wait <= 0:
                self.req_left -= requests
                self.tok_left -= tokens
                return True
            if now + wait > deadline:
                return False                      # would not make it: say so now, not later
            ticket = object()                     # join the line: first come, first served
            self.line.append(ticket)
            self.queued += tokens
        try:
            while True:
                with self.lock:
                    now = time.time()
                    first_in_line = self.line[0] is ticket
                    wait = self._wait_time(tokens, requests, now) if first_in_line else 0.05
                    if first_in_line and wait <= 0:
                        self.req_left -= requests
                        self.tok_left -= tokens
                        return True
                if now + (wait if first_in_line else 0) > deadline:
                    return False
                time.sleep(min(wait, 0.05))
        finally:
            with self.lock:
                self.line.remove(ticket)
                self.queued -= tokens

    def settle(self, booked, used):
        """After a call: give back what we booked but did not use (or take the extra)."""
        if self.enabled:
            with self.lock:
                self.tok_left = min(self.tpm * config.SAFETY, self.tok_left + booked - used)

    def expected_wait(self, tokens, requests=1):
        if not self.enabled:
            return 0.0
        with self.lock:
            return self._wait_time(tokens + self.queued, requests, time.time())

    def provider_said_wait(self, seconds):
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)

    def learn_from_headers(self, headers, rate_limited_on_requests=False):
        """Limits can change without notice, so trust what the provider reports."""
        with self.lock:
            tpm = headers.get("x-ratelimit-limit-tokens")
            if tpm and tpm.isdigit() and int(tpm) != self.tpm:
                self.tpm = int(tpm)
            left = headers.get("x-ratelimit-remaining-tokens")
            if left and left.isdigit():      # the provider's own count wins when it is lower
                self.tok_left = min(self.tok_left, int(left) - (1 - config.SAFETY) * self.tpm)
            if rate_limited_on_requests:            # RPM is not in the headers: back off
                self.rpm = max(1, int(self.rpm * 0.8))


# ====================================================================== daily budget
class DailyBudget:
    """Tokens and requests left for the day. Requests left come from Groq's header; tokens
    left are our own count (no header gives them), started from data/daily_usage.json."""

    def __init__(self, rpd, tpd):
        self.rpd, self.tpd = rpd, tpd
        self.lock = threading.Lock()
        self.tokens_left = tpd
        self.requests_used = 0
        self.header_remaining_requests = None
        self.exhausted_until = 0.0

    def record(self, tokens, headers):
        with self.lock:
            self.tokens_left -= tokens
            self.requests_used += 1
            r = headers.get("x-ratelimit-remaining-requests")
            if r and r.isdigit():
                self.header_remaining_requests = int(r)

    def fraction_left(self):
        with self.lock:
            if time.time() < self.exhausted_until:
                return 0.0
            req_left = (self.header_remaining_requests if self.header_remaining_requests is not None
                        else self.rpd - self.requests_used)
            return max(0.0, min(self.tokens_left / self.tpd, req_left / self.rpd))

    def mark_exhausted(self, seconds):
        with self.lock:
            self.exhausted_until = time.time() + seconds

    def gone_message(self, tokens_per_answer=2100):
        """What to tell the person. The budget refills continuously (tpd / 86400 per second),
        so we can say when the next written answer becomes possible."""
        with self.lock:
            wait = max(self.exhausted_until - time.time(),
                       (tokens_per_answer - self.tokens_left) / (self.tpd / 86400), 60)
        return f"the daily model budget is used up; written answers resume in about {wait / 60:.0f} minutes"


# ====================================================================== the check
class Gate:
    """Holds streamed text and releases it only in pieces that pass the check."""

    def __init__(self, hits, send):
        self.hits, self.send = hits, send
        self.buffer = ""
        self.sent = ""
        self.first_sent_at = None
        self.check_failed = None

    def feed(self, piece):
        self.buffer += piece
        if self.sent == "" and self.buffer.strip().startswith("NOT_IN_PAPERS"):
            raise StopStream("refuse")
        self.drain(final=False)

    def drain(self, final):
        while True:
            m = CITE_RE.search(self.buffer)
            if not m or (m.end() == len(self.buffer) and not final):
                return                  # no citation yet, or wait one more character for "[P1][P2]" / "."
            chunk, self.buffer = self.buffer[:m.end()], self.buffer[m.end():]
            # carry trailing punctuation with the chunk: "... [P2]."
            while self.buffer[:1] in (".", ",", ";", ":"):
                chunk, self.buffer = chunk + self.buffer[0], self.buffer[1:]
            problem = self.check(chunk)
            if problem:
                self.check_failed = problem
                raise StopStream("check")
            self.release(chunk)

    def check(self, chunk):
        ids = [int(x) for x in re.findall(r"\[P(\d+)\]", chunk)]
        if not ids or any(i < 1 or i > len(self.hits) for i in ids):
            return f"cites a passage that was not retrieved: {ids}"
        claim = CITE_RE.sub("", chunk)
        cited_text = " ".join(f"{self.hits[i - 1]['section']} {self.hits[i - 1]['page']} {self.hits[i - 1]['text']}"
                              for i in ids)
        for num in NUM_RE.findall(claim):
            if len(num) >= 2 and num not in cited_text:
                return f"number {num} is not in the cited passage"
        return None

    def release(self, chunk):
        if self.first_sent_at is None:
            self.first_sent_at = time.time()
        self.sent += chunk
        self.send(chunk)

    def finish(self):
        """End of stream. Leftover text with no citation is only kept if it says nothing."""
        self.drain(final=True)
        rest = self.buffer.strip()
        if rest and len(rest.split()) <= 3 and not NUM_RE.search(rest):
            self.release(self.buffer)
        elif rest:
            self.check_failed = self.check_failed or "uncited text at the end was dropped"
        self.buffer = ""


def estimate_draft_tokens(setting):
    """Tokens one drafting call will use, before we know the passages: the passages
    (about 1.7 tokens per word, measured: ~1,800 prompt tokens for 4 x 250 words), the
    instructions, and about 150 tokens of thinking + answer."""
    words, pieces, _ = config.RETRIEVAL_SETTINGS[setting]
    return int(words * pieces * 1.7) + 200 + 150


def readable(text, hits):
    """[P2] -> (Attention Is All You Need, p.4, 3.2 Scaled Dot-Product Attention)"""
    def one(m):
        h = hits[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(hits) else None
        return f"({h['paper_name']}, p.{h['page']}, {h['section']})" if h else m.group(0)
    return re.sub(r"\[P(\d+)\]", one, text)


# ====================================================================== the pipeline
class Relay:
    def __init__(self, base_url, api_key, limits, setting=config.DEFAULT_SETTING,
                 use_limiter=True, retry_policy="smart", record_replay=False, allow_lean=True):
        self.base_url, self.api_key = base_url, api_key
        self.allow_lean = allow_lean   # False for retrieval experiments: always run every step
        self.setting = setting
        self.retrievers = {}
        for name in {setting, config.LEAN_SETTING}:
            size, k, rerank = config.RETRIEVAL_SETTINGS[name]
            self.retrievers[name] = Retriever(size, k, rerank)
        self.limiter = Limiter(limits["rpm"], limits["tpm"], enabled=use_limiter)
        self.budget = DailyBudget(limits["rpd"], limits["tpd"])
        self.retry_policy = retry_policy
        self.record_replay = record_replay
        self.replay = {}
        self.lock = threading.Lock()
        self.waiting_for_first_token = 0

    # ------------------------------------------------------------------ one model call, with retries
    def call_model(self, r, model, messages, max_tokens, deadline, on_text=None, optional=False,
                   first_token_deadline=None, booked=None):
        """Returns CallResult, or raises GiveUp(status) when we should fall back.
        optional=True: a step we can live without -> short timeout, no retries.
        first_token_deadline: never wait for a first token past this moment.
        booked: tokens already booked with the limiter at the door (first attempt only)."""
        max_tokens, extra = config.call_settings(model, max_tokens)   # + room for thinking
        est_out = max_tokens if config.TPM_COUNTS_MAX_TOKENS else min(max_tokens, 150)
        est_tokens = sum(len(m["content"]) for m in messages) // 4 + est_out
        tries_429 = tries_err = 0
        while True:
            r.mark("working")
            if booked is not None:          # first attempt: tokens were booked at the door
                this_booking, booked = booked, None
            elif self.limiter.acquire(est_tokens, deadline):
                this_booking = est_tokens
            else:
                r.mark("waiting")
                raise GiveUp("rate_limited", "too busy: no room under the per-minute limit in time")
            r.mark("waiting")
            timeout = config.OPTIONAL_STEP_TIMEOUT_S if optional else config.CALL_READ_TIMEOUT_S
            if first_token_deadline:
                timeout = max(0.5, min(timeout, first_token_deadline - time.time()))
            try:
                res = stream_chat(self.base_url, self.api_key, model, messages, max_tokens,
                                  on_text=on_text, read_timeout=timeout, extra=extra,
                                  first_answer_by=first_token_deadline)
                r.mark("working")
                r.llm_calls += 1
                r.tokens_in += res.tokens_in
                r.tokens_out += res.tokens_out
                if res.thinking_started_ms is not None and res.ttft_ms is not None:
                    r.thinking_ms += res.ttft_ms - res.thinking_started_ms
                if res.finish_reason == "length" and not res.text:
                    r.check_note = "thinking used up max_tokens: no answer text"
                self.budget.record(res.tokens_in + res.tokens_out, res.headers)
                self.limiter.settle(this_booking, res.tokens_in + res.tokens_out)
                self.limiter.learn_from_headers(res.headers)
                if res.headers.get("x-standin-replayed") == "0":
                    r.invented_text = True     # stand-in made up the text: don't grade it
                if self.record_replay:     # the stand-in replays real text AND real token counts
                    from standin_server import prompt_key
                    with self.lock:
                        self.replay[prompt_key(model, messages)] = {"text": res.text,
                                                                    "tokens_out": res.tokens_out}
                return res
            except RateLimited as e:
                r.mark("retrying")
                r.saw_429 = True
                self.limiter.settle(this_booking, 0)          # a refused call used no tokens
                self.limiter.learn_from_headers(e.headers, rate_limited_on_requests="request" in e.message.lower())
                if e.per_day:
                    self.budget.mark_exhausted(e.retry_after)
                    raise GiveUp("rate_limited", self.budget.gone_message())
                self.limiter.provider_said_wait(e.retry_after)
                if optional:                              # never wait for a step we can skip
                    raise GiveUp("rate_limited", "optional step skipped")
                if self.retry_policy == "naive":          # retry at once, ignore retry-after
                    if tries_429 < 4:
                        tries_429 += 1
                        r.retries += 1
                        continue
                    raise GiveUp("rate_limited", "rate limited")
                wait = e.retry_after + random.uniform(0, 0.3)
                if tries_429 < config.MAX_RETRIES_429 and time.time() + wait < deadline:
                    tries_429 += 1
                    r.retries += 1
                    time.sleep(wait)
                    r.mark("retrying")
                    continue
                raise GiveUp("rate_limited", "rate limited and the wait would be too long")
            except (ServerError, CallTimeout) as e:
                r.mark("retrying")
                got_text = isinstance(e, CallTimeout) and e.got_text
                if got_text:                     # already streaming: never retry, it would repeat text
                    raise GiveUp("timeout", "the model stopped mid-answer")
                if optional:
                    raise GiveUp("error", "optional step skipped")
                limit = 4 if self.retry_policy == "naive" else config.MAX_RETRIES_ERROR
                backoff = 0 if self.retry_policy == "naive" else random.uniform(0.2, 0.6)
                if tries_err < limit and time.time() + backoff < deadline:
                    tries_err += 1
                    r.retries += 1
                    time.sleep(backoff)
                    r.mark("retrying")
                    continue
                raise GiveUp("timeout" if isinstance(e, CallTimeout) else "error", str(e)[:120])

    # ------------------------------------------------------------------ the steps
    def answer(self, question, arrival=None):
        r = Request(question, arrival or time.time())
        with self.lock:
            self.waiting_for_first_token += 1
            r.queue_at_arrival = self.waiting_for_first_token
        try:
            self._run(r)
        finally:
            with self.lock:
                self.waiting_for_first_token -= 1
        return r.finish()

    def _run(self, r):
        r.mark("waiting")              # any gap between arrival and starting work is queueing
        deadline_first_token = r.arrival + config.PATIENCE_S
        draft_deadline = deadline_first_token - config.DRAFT_RESERVE_S

        # 1. ADMIT -----------------------------------------------------------
        left = self.budget.fraction_left()
        if left <= 0:
            return self.fallback(r, "rate_limited", self.budget.gone_message())
        daily_low = left < config.LEAN_WHEN_DAILY_LEFT
        r.setting = config.LEAN_SETTING if daily_low and self.allow_lean else self.setting
        retriever = self.retrievers[r.setting]
        understand_tokens = (len(UNDERSTAND_SYSTEM) + len(r.question)) // 4 + 150
        draft_tokens = estimate_draft_tokens(r.setting)
        minute_busy = self.limiter.expected_wait(understand_tokens + draft_tokens, requests=2) > 1.0
        r.mode = "lean" if (daily_low or minute_busy) and self.allow_lean else "full"
        # Book the whole request's tokens NOW, before spending any. In the first real burst,
        # 5 of 10 requests paid for the understand call and were then turned away at drafting.
        booking = draft_tokens + (understand_tokens if r.mode == "full" else 0)
        if not self.limiter.acquire(booking, draft_deadline, requests=2 if r.mode == "full" else 1):
            r.mark("waiting")
            return self.fallback(r, "rate_limited", "too busy: no room under the per-minute limit in time")
        r.mark("waiting")

        # 2. UNDERSTAND (optional) -------------------------------------------
        paper, search, on_topic = None, r.question, True
        if r.mode == "full":
            try:
                res = self.call_model(r, config.MODEL_UNDERSTAND, [
                    {"role": "system", "content": UNDERSTAND_SYSTEM},
                    {"role": "user", "content": f"Question: {r.question}"}], 60, draft_deadline,
                    optional=True, first_token_deadline=time.time() + config.OPTIONAL_STEP_TIMEOUT_S,
                    booked=understand_tokens)
                m = re.search(r"\{.*\}", res.text, re.S)
                info = json.loads(m.group(0)) if m else {}
                on_topic = info.get("on_topic", True) is not False
                paper = info.get("paper") if info.get("paper") in ("attention", "moshi", "hnet") else None
                search = f"{r.question} {info.get('search', '')}"
            except (GiveUp, ValueError):
                pass                                # not essential: carry on with the raw question
        r.mark("working")

        # 3. RETRIEVE (tool) + LOOP ------------------------------------------
        hits, top = retriever.search(search, paper)
        r.searches = 1
        if top < config.WEAK_RETRIEVAL_SCORE and paper:
            hits, top = retriever.search(search)          # widen: drop the paper filter
            r.searches = 2
        r.hits, r.top_score = hits, round(top, 2)
        r.mark("working")

        # 4. DECIDE (branch) --------------------------------------------------
        if not hits or (top < config.WEAK_RETRIEVAL_SCORE and not on_topic):
            r.mode = "refused_early"
            self.limiter.settle(draft_tokens, 0)        # no draft call: give its booking back
            r.send(REFUSAL)
            return

        # 5. DRAFT + CHECK ----------------------------------------------------
        if self.budget.fraction_left() <= 0:
            self.limiter.settle(draft_tokens, 0)
            return self.fallback(r, "rate_limited", self.budget.gone_message(), hits)
        passages = "\n\n".join(
            f"[P{i}] ({h['paper_name']}, page {h['page']}, section: {h['section']})\n{h['text']}"
            for i, h in enumerate(hits, 1))
        messages = [{"role": "system", "content": DRAFT_SYSTEM},
                    {"role": "user", "content": f"Passages:\n{passages}\n\nQuestion: {r.question}"}]
        gate = Gate(hits, lambda chunk: r.send(readable(chunk, hits)))
        max_tokens = config.LEAN_MAX_TOKENS if r.mode == "lean" else config.ANSWER_MAX_TOKENS
        try:
            res = self.call_model(r, config.MODEL_DRAFT, messages, max_tokens, draft_deadline,
                                  on_text=gate.feed, first_token_deadline=deadline_first_token - 0.3,
                                  booked=draft_tokens)
            r.model_ttft_ms = res.ttft_ms
            gate.finish()
        except StopStream as s:
            r.mark("working")
            r.llm_calls += 1
            est_in = sum(len(m["content"]) for m in messages) // 4
            est_out = len(gate.sent + gate.buffer) // 4
            r.tokens_in += est_in                               # call was cut short: estimate
            r.tokens_out += est_out
            self.budget.record(est_in + est_out, {})
            self.limiter.settle(draft_tokens, est_in + est_out)
            if self.record_replay:        # the stand-in must be able to refuse (or fail the check) too
                from standin_server import prompt_key
                with self.lock:
                    self.replay[prompt_key(config.MODEL_DRAFT, messages)] = {
                        "text": gate.sent + gate.buffer, "tokens_out": est_out}
            if str(s) == "refuse":
                r.mode = "refused"
                r.send(REFUSAL)
                return
        except GiveUp as g:
            if gate.sent:                 # part of a checked answer already went out
                r.status = g.status
                return
            return self.fallback(r, g.status, g.reason, hits)
        r.check_note = gate.check_failed or r.check_note
        if not gate.sent:                 # the check rejected everything
            return self.fallback(r, "ok", "I couldn't verify a written answer against the papers", hits)

    def fallback(self, r, status, reason, hits=None):
        """Passages-only reply: no model call, so it always works, even with no budget."""
        if hits is None:
            hits, _ = self.retrievers[config.LEAN_SETTING].search(r.question)
            r.hits = hits
        r.status, r.mode, r.reason = status, "passages_only", reason
        lines = [f"{FALLBACK} ({reason}). These passages from the papers look most related. "
                 "They may not answer your question:"]
        for h in hits[:2]:
            lines.append(f'- "{h["text"][:280]}..." ({h["paper_name"]}, p.{h["page"]}, {h["section"]})')
        r.send("\n".join(lines))


class GiveUp(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Request:
    """Everything we measure about one request."""

    def __init__(self, question, arrival):
        self.question, self.arrival = question, arrival
        self.status, self.mode, self.reason, self.setting = "ok", "", "", ""
        self.text = ""
        self.first_token_at = None
        self.retries = self.llm_calls = self.tokens_in = self.tokens_out = self.searches = 0
        self.saw_429 = False
        self.invented_text = False
        self.thinking_ms = 0.0     # time the model spent thinking before its answer started
        self.model_ttft_ms = None
        self.top_score = 0.0
        self.hits = []
        self.check_note = None
        self.queue_at_arrival = 0
        self.phases = {"waiting": 0.0, "working": 0.0, "retrying": 0.0}
        self._last = arrival

    def mark(self, kind):
        """Add the time since the last mark to waiting, working or retrying."""
        now = time.time()
        self.phases[kind] += (now - self._last) * 1000
        self._last = now

    def send(self, text):
        if self.first_token_at is None:
            self.first_token_at = time.time()
        self.text += text

    def finish(self):
        self.mark("working")
        end = time.time()
        first_ms = (self.first_token_at - self.arrival) * 1000 if self.first_token_at else None
        if first_ms is None or first_ms > config.PATIENCE_S * 1000:
            self.status = "timeout"              # the person had already given up
        return {
            "status": self.status, "mode": self.mode, "reason": self.reason, "setting": self.setting,
            "first_token_ms": round(first_ms) if first_ms is not None else "",
            "total_ms": round((end - self.arrival) * 1000),
            "retries": self.retries, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "llm_calls": self.llm_calls, "searches": self.searches, "saw_429": self.saw_429,
            "model_ttft_ms": round(self.model_ttft_ms) if self.model_ttft_ms else "",
            "waiting_ms": round(self.phases["waiting"]), "working_ms": round(self.phases["working"]),
            "retrying_ms": round(self.phases["retrying"]), "queue_at_arrival": self.queue_at_arrival,
            "top_score": self.top_score, "check_note": self.check_note or "",
            "invented_text": self.invented_text, "thinking_ms": round(self.thinking_ms),
            "retrieved_papers": sorted({h["paper"] for h in self.hits}), "text": self.text,
        }
