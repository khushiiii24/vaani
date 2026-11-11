"""
The stand-in model service: a fake Groq that I control.

It speaks the same streaming API as Groq and can:
  - be slow          (first-token time drawn from the distribution measured on Groq)
  - fail             (some calls return HTTP 500/503)
  - refuse at a limit(429 with retry-after, like Groq, for RPM / TPM / RPD / TPD)
  - get slower under load (each request in flight beyond `capacity` adds delay)
  - succeed slowly   (a few calls are many times slower than normal)
  - stream           (sends the reply a few tokens at a time)

What text does it send? If the same prompt was sent to real Groq before, it
replays Groq's recorded answer (data/replay.json). Otherwise it makes a short
answer from the first passage in the prompt.

Run on its own:   python standin_server.py --scale 10
(run_load.py also starts it automatically for stand-in scenarios.)
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PERIOD = {"rpm": 60, "tpm": 60, "rpd": 86400, "tpd": 86400}   # seconds to refill each limit

DEFAULTS = {
    # timing, overwritten by data/calibration.json (measured on real Groq)
    "ttft_p50_ms": 250, "ttft_p95_ms": 700,     # first-token time spread (log-normal)
    "thinking_ms": 70,                          # last part of that wait spent "thinking" (gpt-oss)
    "tokens_per_s": 500,                        # streaming speed after first token
    # failure behaviour (my choice; real Groq rarely failed in the probe)
    "error_rate": 0.02, "slow_rate": 0.03, "slow_factor": 10,
    # load behaviour: past `capacity` requests in flight, each extra one adds 15% delay
    "capacity": 8, "slowdown_per_extra": 0.15,
    # limits
    "rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000,
    "tpm_counts_max_tokens": False,
    "seed": 7,
}


def prompt_key(model, messages):
    return hashlib.sha1(json.dumps([model, messages], sort_keys=True).encode()).hexdigest()


class StandIn:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        # Limits are buckets that refill continuously, like Groq's (measured: RPM 429 "try again
        # in 2s" = 60/30; TPM 429 "14.3s" = missing tokens / (8000/60); daily requests come back
        # one per 86.4 s). Each starts full and refills at limit / period per second.
        self.level = {k: float(cfg[k]) for k in PERIOD}
        self.last = time.time()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.rng = random.Random(cfg["seed"])
        # log-normal: median = p50, and p95 = median * exp(1.645 * sigma)
        self.mu = math.log(cfg["ttft_p50_ms"])
        self.sigma = max(0.05, math.log(cfg["ttft_p95_ms"] / cfg["ttft_p50_ms"]) / 1.645)
        self.replay = {}
        if os.path.exists("data/replay.json"):
            with open("data/replay.json", encoding="utf-8") as f:
                self.replay = json.load(f)

    # ------------------------------------------------------------ limits
    def _refill(self):
        now = time.time()
        dt, self.last = now - self.last, now
        for k, period in PERIOD.items():
            self.level[k] = min(self.cfg[k], self.level[k] + dt * self.cfg[k] / period)

    def check_limits(self, model, tokens):
        """Return None if allowed (and take what it uses), else (retry_after_s, kind, message)."""
        with self.lock:
            self._refill()
            for k, need, kind, name in (("rpd", 1, "requests", "requests per day (RPD)"),
                                        ("tpd", tokens, "tokens", "tokens per day (TPD)"),
                                        ("rpm", 1, "requests", "requests per minute (RPM)"),
                                        ("tpm", tokens, "tokens", "tokens per minute (TPM)")):
                if self.level[k] < need:
                    wait = (need - self.level[k]) / (self.cfg[k] / PERIOD[k])
                    used = self.cfg[k] - self.level[k]
                    return wait, kind, (f"Rate limit reached for model `{model}` (stand-in) on {name}: "
                                        f"Limit {self.cfg[k]}, Used {used:.0f}, Requested {need}. "
                                        f"Please try again in {wait:.2f}s.")
            self.level["rpd"] -= 1
            self.level["rpm"] -= 1
            self.level["tpd"] -= tokens
            self.level["tpm"] -= tokens
            return None

    def charge(self, tokens):
        """Output tokens are counted when they are produced, after the request was admitted."""
        with self.lock:
            self.level["tpm"] -= tokens
            self.level["tpd"] -= tokens

    def headers(self):
        with self.lock:
            self._refill()
            return {
                "x-ratelimit-limit-requests": str(self.cfg["rpd"]),
                "x-ratelimit-remaining-requests": str(max(0, int(self.level["rpd"]))),
                "x-ratelimit-limit-tokens": str(self.cfg["tpm"]),
                "x-ratelimit-remaining-tokens": str(max(0, int(self.level["tpm"]))),
                "x-ratelimit-reset-tokens": f"{(self.cfg['tpm'] - self.level['tpm']) / (self.cfg['tpm'] / 60):.2f}s",
            }

    # ------------------------------------------------------------ reply text
    def reply_text(self, model, messages):
        """Returns (text, output tokens). Output tokens include the thinking tokens real
        Groq reported for the same prompt; None means "estimate from the text"."""
        key = prompt_key(model, messages)
        if key in self.replay:
            rec = self.replay[key]
            return (rec["text"], rec.get("tokens_out")) if isinstance(rec, dict) else (rec, None)
        user = messages[-1]["content"]
        if "Question:" in user and "[P1]" in user:     # drafting prompt: quote the first passage
            first = user.split("[P1]", 1)[1].split("\n", 1)[1]      # skip the "(paper, page)" line
            sentences = re.split(r"(?<=\.)\s", first.strip())
            sentence = next((s for s in sentences if len(s.split()) >= 6), sentences[0])[:300]
            return f"{sentence.rstrip('.')} [P1].", None
        question = user.split("Question:")[-1].strip()      # understanding prompt
        return json.dumps({"on_topic": True, "paper": "any", "search": question}), None


def make_handler(si):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send_json(self, code, obj, extra_headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            messages, model = req["messages"], req["model"]
            prompt_tokens = sum(len(m["content"]) for m in messages) // 4 + 8
            replayed = prompt_key(model, messages) in si.replay
            text, recorded_tokens = si.reply_text(model, messages)
            out_tokens = recorded_tokens or min(req.get("max_tokens", 300), max(1, len(text) // 4))

            # 1) limits. Admission counts the prompt (Groq's 429 said "Requested 1983" for a
            #    ~1,900-token prompt with max_tokens 700, so max_tokens is NOT booked up front);
            #    the output is charged when it is produced. tpm_counts_max_tokens copies the probe.
            up_front = req.get("max_tokens", 300) if si.cfg["tpm_counts_max_tokens"] else 0
            refused = si.check_limits(model, prompt_tokens + up_front)
            if refused:
                wait, kind, msg = refused
                hdr = si.headers()
                hdr["retry-after"] = str(max(1, math.ceil(wait)))
                return self.send_json(429, {"error": {"message": msg, "type": kind,
                                                      "code": "rate_limit_exceeded"}}, hdr)
            # 2) random failure
            if si.rng.random() < si.cfg["error_rate"]:
                time.sleep(si.rng.uniform(0.05, 0.4))
                return self.send_json(si.rng.choice([500, 503]),
                                      {"error": {"message": "internal error (stand-in)"}})

            with si.lock:
                si.in_flight += 1
                si.peak_in_flight = max(si.peak_in_flight, si.in_flight)
                load = 1 + si.cfg["slowdown_per_extra"] * max(0, si.in_flight - si.cfg["capacity"])
            try:
                ttft = math.exp(si.rng.gauss(si.mu, si.sigma)) / 1000 * load
                if si.rng.random() < si.cfg["slow_rate"]:
                    ttft *= si.cfg["slow_factor"]          # succeeds, but slowly
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                for k, v in si.headers().items():
                    self.send_header(k, v)
                self.send_header("x-standin-replayed", "1" if replayed else "0")  # 0 = invented text
                self.end_headers()
                if "gpt-oss" in model:      # thinking models: silent first, then a short burst of
                    think = min(ttft, si.cfg["thinking_ms"] / 1000)   # reasoning, then the answer
                    time.sleep(ttft - think)     # (measured on Groq: thinking is ~50-120 ms of the wait)
                    start = time.time()
                    while time.time() - start < think:
                        self.write_event({"choices": [{"index": 0, "delta": {"reasoning": "thinking "}}]})
                        time.sleep(min(0.02, think))
                else:
                    time.sleep(ttft)
                # 3) stream the reply ~4 characters (one token) at a time, in small groups
                step = 12
                per_piece = (step / 4) / (si.cfg["tokens_per_s"] / load)
                for i in range(0, len(text), step):
                    self.write_event({"choices": [{"index": 0, "delta": {"content": text[i:i + step]}}]})
                    time.sleep(per_piece)
                if not si.cfg["tpm_counts_max_tokens"]:
                    si.charge(out_tokens)
                self.write_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                  "x_groq": {"usage": {"prompt_tokens": prompt_tokens,
                                                       "completion_tokens": out_tokens}}})
                self.write_chunk(b"data: [DONE]\n\n")
                self.write_chunk(b"")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with si.lock:
                    si.in_flight -= 1

        def write_event(self, obj):
            self.write_chunk(f"data: {json.dumps(obj)}\n\n".encode())

        def write_chunk(self, data):
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256    # default backlog is 5: a burst of 30 connections would sit in
                                # TCP retry (1 s, 2 s, 4 s ...) and look like a slow model

    def handle_error(self, request, client_address):
        pass                    # a client that hung up (timeout) is normal here, not an error


def start(overrides=None, port=8100):
    """Start the stand-in in a background thread. Returns (server, StandIn)."""
    cfg = dict(DEFAULTS)
    if os.path.exists("data/calibration.json"):
        with open("data/calibration.json", encoding="utf-8") as f:
            cal = json.load(f)
        for k in ("ttft_p50_ms", "ttft_p95_ms", "thinking_ms", "tokens_per_s", "rpm", "tpm", "rpd", "tpd",
                  "error_rate", "tpm_counts_max_tokens"):
            if k in cal and cal[k] is not None:
                cfg[k] = cal[k]
    cfg.update(overrides or {})
    si = StandIn(cfg)
    server = QuietServer(("127.0.0.1", port), make_handler(si))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, si


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0, help="multiply per-minute limits")
    ap.add_argument("--port", type=int, default=8100)
    a = ap.parse_args()
    server, si = start(port=a.port)
    si.cfg["rpm"] = int(si.cfg["rpm"] * a.scale)
    si.cfg["tpm"] = int(si.cfg["tpm"] * a.scale)
    print("stand-in running on port", a.port, "with", si.cfg)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
