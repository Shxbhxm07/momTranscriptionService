"""IBM Cloud IAM tokens for watsonx.ai.

WHY THIS EXISTS. Every other backend this service talks to (OpenRouter, Groq, a local vLLM) takes a
static API key, which is what GroqKeyPool holds and what generate() puts straight into the
Authorization header. watsonx.ai native auth does not work that way: the API key is exchanged for a
bearer token that EXPIRES, typically after an hour. Put the API key in the header and every call is
401; fetch a token once and calls start failing mid-meeting instead — which on a 20-minute job means
a half-written MoM. So the token is cached and re-fetched shortly before it expires.

A Cloud Pak for Data (on-prem) Zen key does not expire and needs none of this — set
LLM_AUTH_MODE=bearer and the normal key path is used.
"""
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)


class IAMTokenCache:
    """Thread-safe IBM Cloud IAM token, refreshed before it expires.

    `skew` is how long before real expiry a token is treated as stale. It must be larger than the
    longest single request: a token that passes the check and then expires while a 60-90 s
    extraction call is in flight fails the call, and the retry costs a minute of a job that is
    already the slowest part of the pipeline.
    """

    def __init__(self, apikey: str, iam_url: str = "https://iam.cloud.ibm.com/identity/token",
                 skew: int = 300, client: httpx.Client = None, now=time.time):
        self._apikey = apikey
        self._iam_url = iam_url
        self._skew = skew
        self._client = client or httpx.Client(timeout=30)
        self._now = now
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and self._now() < self._expires_at - self._skew:
                return self._token
            r = self._client.post(
                self._iam_url,
                data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self._apikey},
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._expires_at = self._now() + float(body.get("expires_in", 3600))
            logger.info("[IBM] IAM token refreshed — valid for %ss", body.get("expires_in", "?"))
            return self._token
