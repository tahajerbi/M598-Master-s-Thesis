"""Polite, cached, resumable HTTP client for the ccMixter Query API 2.0.

Design notes
------------
* One request per second by default. ccMixter is a small volunteer-run site;
  the API is open for third-party use but /api/ is disallowed in robots.txt for
  generic bots, so we identify ourselves clearly and cache aggressively.
* Every successful response is cached on disk keyed by the canonical URL, so
  rerunning any phase is free and idempotent.
* Two distinct failure modes, handled differently:
    - Transient (timeouts, 5xx, connection resets) -> retry with backoff.
    - Malformed JSON -> NOT retried. Verified in practice: ccMixter returns
      byte-identical broken JSON on every attempt for certain id ranges, so
      four retries just cost four requests. Raised as MalformedResponse so the
      caller can bisect the batch and isolate the offending record.
  The raw text of a malformed response is written to data/bad_payloads/ so the
  failure can actually be diagnosed rather than guessed at.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

# ccMixter sometimes returns a response whose header section blows past Python's
# default 64 KiB line limit. Raising it lets the response through.
http.client._MAXLINE = 10 * 1024 * 1024

log = logging.getLogger(__name__)

API_URL = "https://ccmixter.org/api/query"

# Matches a JSON number written with one or more leading zeros, as ccHost does
# for sub-100 BPM values: `"bpm" : 092` -> keep the key/colon, drop the zeros.
# Anchored on the preceding `:` so it can't touch strings or legitimate zeros.
_BPM_LEADING_ZERO = re.compile(r'(:\s*)0+(\d)')


class MalformedResponse(RuntimeError):
    """The server replied, but the body is not valid JSON. Not worth retrying."""


class RateLimiter:
    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class CCMixterClient:
    def __init__(
        self,
        contact: str,
        cache_dir: Path,
        min_interval: float = 1.0,
        timeout: int = 60,
        max_retries: int = 4,
        offline: bool = False,
    ) -> None:
        if not contact or "@" not in contact:
            raise ValueError(
                "Pass a real contact email (--contact you@example.com). It goes in "
                "the User-Agent so the site admins can reach you."
            )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bad_dir = self.cache_dir.parent / "bad_payloads"
        self.limiter = RateLimiter(min_interval)
        self.timeout = timeout
        self.max_retries = max_retries
        self.offline = offline
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "ccmixter-remix-graph-research/0.1 (MSc thesis, Gisma University; "
                    f"contact: {contact})"
                ),
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.stats = {"hits": 0, "fetches": 0, "errors": 0, "malformed": 0, "salvaged": 0}

    # -- cache ---------------------------------------------------------------

    def _canonical(self, params: dict[str, Any]) -> str:
        items = sorted((k, str(v)) for k, v in params.items() if v is not None)
        return f"{API_URL}?{urlencode(items)}"

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json.gz"

    def _read_cache(self, path: Path):
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None

    def _write_cache(self, path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)

    def _dump_bad(self, url: str, text: str, exc: Exception) -> Path:
        self.bad_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(url.encode()).hexdigest()[:12]
        path = self.bad_dir / f"{digest}.txt"
        path.write_text(
            f"# url: {url}\n# error: {exc}\n# length: {len(text)}\n\n{text}",
            encoding="utf-8",
            errors="replace",
        )
        return path

    # -- fetching ------------------------------------------------------------

    def get_json(self, **params) -> list[dict]:
        params.setdefault("f", "json")
        url = self._canonical(params)
        path = self._cache_path(url)

        cached = self._read_cache(path)
        if cached is not None:
            self.stats["hits"] += 1
            return self._normalise(cached)

        if self.offline:
            raise RuntimeError(f"offline mode and not cached: {url}")

        last_err = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                resp = self.session.get(API_URL, params=params, timeout=self.timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"status {resp.status_code}")
                resp.raise_for_status()
                text = resp.text.strip()
            except requests.RequestException as exc:
                last_err = exc
                backoff = 2**attempt * 2
                log.warning("fetch failed (%s), retry in %ss: %s", exc, backoff, url)
                time.sleep(backoff)
                continue

            try:
                payload = json.loads(text) if text else []
            except json.JSONDecodeError:
                # ccHost emits BPM as a zero-padded, UNQUOTED number, e.g.
                #   "bpm" : 092
                # A leading zero on an integer is illegal JSON, so strict parsers
                # reject the whole row. This is a deterministic upstream bug, not
                # a transient error, so retrying is pointless - but it is cleanly
                # repairable. Strip the offending leading zeros and reparse. If it
                # still fails, it is some other breakage and we give up honestly.
                try:
                    repaired = _BPM_LEADING_ZERO.sub(r'\g<1>\g<2>', text)
                    payload = json.loads(repaired) if repaired else []
                    self.stats["salvaged"] += 1
                except json.JSONDecodeError as exc2:
                    self.stats["malformed"] += 1
                    dumped = self._dump_bad(url, text, exc2)
                    raise MalformedResponse(
                        f"invalid JSON from {url} ({exc2}); raw body saved to {dumped}"
                    ) from exc2

            self.stats["fetches"] += 1
            self._write_cache(path, payload)
            return self._normalise(payload)

        self.stats["errors"] += 1
        raise RuntimeError(f"giving up after {self.max_retries} attempts: {url} ({last_err})")

    @staticmethod
    def _normalise(payload) -> list[dict]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("results", "rows", "data"):
                if isinstance(payload.get(key), list):
                    return [r for r in payload[key] if isinstance(r, dict)]
            if "upload_id" in payload or "id" in payload:
                return [payload]
        return []
