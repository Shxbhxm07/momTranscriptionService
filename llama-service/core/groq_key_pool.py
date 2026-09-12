import os
import time
import threading
import logging

logger = logging.getLogger(__name__)


class GroqKeyPool:
    """Round-robin pool of Groq API keys with per-key cooldown after a 429."""

    def __init__(self, keys):
        self._keys = keys
        self._lock = threading.Lock()
        self._index = 0
        self._cooldowns = {}
        # Keys rejected with 401/403 (revoked/invalid). Unlike a 429 cooldown this never
        # expires — a revoked key will not start working again, so retrying it only burns
        # requests. Held in memory, so editing GROQ_API_KEYS + restart re-enables the key.
        self._disabled = set()

    def __len__(self):
        return len(self._keys)

    def acquire(self):
        """Return (key, fresh). fresh=False means every key is currently
        cooling down and the returned key is just the soonest to recover.
        Permanently-disabled (401/403) keys are never returned unless every
        key is disabled, in which case the caller is allowed to fail loudly."""
        with self._lock:
            now = time.time()
            live = [k for k in self._keys if k not in self._disabled]
            if not live:
                return self._keys[0], False
            for _ in range(len(self._keys)):
                key = self._keys[self._index]
                self._index = (self._index + 1) % len(self._keys)
                if key in self._disabled:
                    continue
                if self._cooldowns.get(key, 0) <= now:
                    return key, True
            key = min(live, key=lambda k: self._cooldowns.get(k, 0))
            return key, False

    def cooldown(self, key, seconds):
        with self._lock:
            self._cooldowns[key] = time.time() + max(seconds, 1)

    def disable(self, key):
        """Drop a key from rotation for the lifetime of the process (401/403 only)."""
        with self._lock:
            self._disabled.add(key)

    def pool_status(self) -> dict:
        """Return {total, available, cooling, disabled} counts for health/logging."""
        now = time.time()
        with self._lock:
            disabled = len(self._disabled)
            cooling = sum(1 for k, t in self._cooldowns.items()
                          if t > now and k not in self._disabled)
            return {
                "total": len(self._keys),
                "available": len(self._keys) - cooling - disabled,
                "cooling": cooling,
                "disabled": disabled,
            }


# The key this service sends is no longer a Groq key: on watsonx it is an IBM Cloud API key, which
# the IAM exchange turns into a bearer token. WATSONX_API_KEY is therefore the name to use, with
# LLM_API_KEY for anything else; GROQ_API_KEYS is still read last so an existing deployment does
# not break on upgrade. Whichever is set first wins, and all three accept a comma-separated list.
KEY_VARS = ("WATSONX_API_KEY", "LLM_API_KEY", "GROQ_API_KEYS")


def load_pool_from_env(multi_var=None):
    """Build a key pool from WATSONX_API_KEY, LLM_API_KEY or GROQ_API_KEYS (comma-separated).
    Returns None if none is set or none yields valid keys. Duplicate keys are silently removed."""
    for var in ((multi_var,) if multi_var else KEY_VARS):
        raw = os.getenv(var, "")
        keys = list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))
        if keys:
            logger.info(f"[KeyPool] {len(keys)} key(s) loaded from {var}")
            return GroqKeyPool(keys)
    return None
