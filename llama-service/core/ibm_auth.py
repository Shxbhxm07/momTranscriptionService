"""Bearer tokens for IBM watsonx — Cloud Pak for Data (on-prem) and IBM Cloud (SaaS).

WHY THIS EXISTS. Every other backend this service talks to — OpenRouter, Groq, a local vLLM — takes
a static API key, which is what GroqKeyPool holds and what generate() puts straight into the
Authorization header. watsonx does not: the credential is exchanged for a token that EXPIRES. Send
the key itself and every call is 401; fetch a token once and calls start failing mid-meeting, which
on a 20-minute job means a half-written MoM. So tokens are cached and re-fetched before they expire.

Two issuers, because the deployment decides:
  • Cloud Pak for Data (the IAF cluster): POST {username, api_key} to /icp4d-api/v1/authorize,
    token comes back as {"token": ...}. No expiry is reported, so it is refreshed on a fixed TTL.
  • IBM Cloud SaaS: the apikey grant against iam.cloud.ibm.com, which does report expires_in.
"""
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)


class _TokenCache:
    """Shared caching. `skew` must exceed the slowest single request: a token that passes the check
    and then expires during a 60-90 s extraction call fails it, and the retry costs a minute of the
    slowest stage in the pipeline."""

    def __init__(self, skew: int = 300, client: httpx.Client = None, now=time.time, verify: bool = True):
        self._skew = skew
        self._client = client or httpx.Client(timeout=30, verify=verify)
        self._now = now
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and self._now() < self._expires_at - self._skew:
                return self._token
            self._token, ttl = self._fetch()
            self._expires_at = self._now() + ttl
            logger.info("[IBM] %s token refreshed — good for %ss", type(self).__name__, int(ttl))
            return self._token

    def _fetch(self):
        raise NotImplementedError


class CP4DTokenCache(_TokenCache):
    """Cloud Pak for Data. The response carries no expiry, so the TTL is configured rather than
    read; CP4D tokens are commonly 12 h, and refreshing early costs one cheap call."""

    def __init__(self, auth_url: str, username: str, api_key: str, ttl: int = 3600, **kw):
        super().__init__(**kw)
        self._auth_url, self._username, self._api_key, self._ttl = auth_url, username, api_key, ttl

    def _fetch(self):
        r = self._client.post(self._auth_url,
                              headers={"Content-Type": "application/json", "Accept": "application/json"},
                              json={"username": self._username, "api_key": self._api_key})
        r.raise_for_status()
        return r.json()["token"], float(self._ttl)


class IAMTokenCache(_TokenCache):
    """IBM Cloud SaaS. Reports expires_in, so the real lifetime is used."""

    def __init__(self, apikey: str, iam_url: str = "https://iam.cloud.ibm.com/identity/token", **kw):
        super().__init__(**kw)
        self._apikey, self._iam_url = apikey, iam_url

    def _fetch(self):
        r = self._client.post(
            self._iam_url,
            data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self._apikey},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        r.raise_for_status()
        body = r.json()
        return body["access_token"], float(body.get("expires_in", 3600))
