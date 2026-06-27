"""Embedding client. Sync, called via asyncio.to_thread by the bot.

Retries 5xx and network errors with exponential backoff. 4xx (auth, bad
request) fails immediately — retrying won't help.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class EmbeddingError(RuntimeError):
    pass


def embed(
    text: str,
    *,
    api_key: str,
    model: str,
    url: str,
    retries: int = 3,
    base_delay: float = 1.0,
    timeout: int = 30,
) -> list[float]:
    """POST a single string to an OpenAI-compatible /v1/embeddings endpoint.

    The caller is responsible for filtering empty strings — we don't paper
    over them with a zero vector, because that would silently pollute the DB
    with a fingerprint that collides with nothing.
    """
    payload = json.dumps({"input": text, "model": model}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Defensive: most OpenAI-compat servers only return 200 here,
                # but a 5xx inside the 200 envelope would be invisible to
                # HTTPError. Treat any 5xx as retryable.
                if 500 <= resp.status < 600:
                    raise EmbeddingError(f"server {resp.status}")
                body = json.loads(resp.read().decode("utf-8"))
            try:
                return body["data"][0]["embedding"]
            except (KeyError, IndexError, TypeError) as exc:
                raise EmbeddingError(f"unexpected response shape: {body!r}") from exc
        except urllib.error.HTTPError as exc:
            last_err = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                # 4xx other than 429 (rate limit) won't fix itself on retry.
                raise EmbeddingError(f"client error {exc.code}: {exc.reason}") from exc
            if attempt == retries:
                break
            time.sleep(base_delay * (2 ** attempt))
        except (urllib.error.URLError, EmbeddingError) as exc:
            last_err = exc
            if attempt == retries:
                break
            time.sleep(base_delay * (2 ** attempt))
    raise EmbeddingError(
        f"embed failed after {retries + 1} attempts: {last_err!r}"
    ) from last_err
