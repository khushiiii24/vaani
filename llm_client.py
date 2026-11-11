"""
One function, stream_chat(), that calls an OpenAI-style chat endpoint with
streaming. It works the same for real Groq and for the stand-in service.

It raises a clear error for each way a call can go wrong, so the pipeline
can decide what to do (wait, retry, fall back).
"""
import json
import time

import requests


class RateLimited(Exception):
    def __init__(self, retry_after, per_day, message, headers):
        super().__init__(message)
        self.retry_after = retry_after   # seconds, from the retry-after header
        self.per_day = per_day           # True if the daily limit was hit
        self.message = message
        self.headers = headers


class ServerError(Exception):
    pass


class CallTimeout(Exception):
    def __init__(self, got_text):
        super().__init__("model call went silent")
        self.got_text = got_text   # text received before it went silent (may be "")


class CallResult:
    def __init__(self):
        self.text = ""
        self.ttft_ms = None        # time to first token of THIS call
        self.total_ms = None
        self.tokens_in = 0
        self.tokens_out = 0          # includes thinking tokens for reasoning models
        self.thinking_started_ms = None   # when the model started thinking (reasoning models)
        self.finish_reason = None    # "length" = ran out of max_tokens
        self.headers = {}


def stream_chat(base_url, api_key, model, messages, max_tokens, on_text=None,
                read_timeout=6.0, temperature=0.0, extra=None, first_answer_by=None):
    """Call the model and stream the reply. on_text(piece) is called for each piece of the
    answer. extra = model-specific request fields, e.g. {"reasoning_effort": "low"}.
    first_answer_by = wall-clock time; if the answer has not started by then, give up
    (a thinking model keeps sending bytes, so the silence timeout alone never fires)."""
    t0 = time.monotonic()
    result = CallResult()
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature, "stream": True, **(extra or {})},
            stream=True,
            timeout=(5.0, read_timeout),   # connect timeout, then max silence between bytes
        )
    except requests.exceptions.Timeout:
        raise CallTimeout("")
    except requests.exceptions.ConnectionError as e:
        raise ServerError(f"connection error: {e}")

    result.headers = {k.lower(): v for k, v in resp.headers.items()}
    resp.encoding = "utf-8"   # the stream declares no charset; requests would guess Latin-1 and
                              # turn "α" into "Î±" (seen in the first real runs)

    if resp.status_code == 429:
        body = resp.text
        try:
            message = json.loads(body)["error"]["message"]
        except Exception:
            message = body[:300]
        retry_after = float(result.headers.get("retry-after", "2") or 2)
        low = message.lower()
        per_day = "per day" in low or "(rpd)" in low or "(tpd)" in low
        raise RateLimited(retry_after, per_day, message, result.headers)
    if resp.status_code >= 500:
        raise ServerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise ServerError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    usage = None
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage") or usage
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                result.finish_reason = choice.get("finish_reason") or result.finish_reason
                if delta.get("reasoning") and result.thinking_started_ms is None:
                    result.thinking_started_ms = (time.monotonic() - t0) * 1000
                if first_answer_by and result.ttft_ms is None and time.time() > first_answer_by:
                    raise CallTimeout("")        # still thinking, and the person is about to leave
                piece = delta.get("content")      # only the answer is shown, never the thinking
                if piece:
                    if result.ttft_ms is None:
                        result.ttft_ms = (time.monotonic() - t0) * 1000
                    result.text += piece
                    if on_text:
                        on_text(piece)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError):
        raise CallTimeout(result.text)
    finally:
        resp.close()

    result.total_ms = (time.monotonic() - t0) * 1000
    if usage:
        result.tokens_in = usage.get("prompt_tokens", 0)
        result.tokens_out = usage.get("completion_tokens", 0)
    else:  # estimate when the server did not report usage (about 4 characters per token)
        result.tokens_in = sum(len(m["content"]) for m in messages) // 4
        result.tokens_out = len(result.text) // 4
    return result
