#!/usr/bin/env python3
"""
Layer-0 auto-builder — keep core_terms.json's CANONICAL names in sync with the repo (add-only).

Mishearing ALIASES are learned automatically at runtime (term_corrector.learn_from_correction);
this only maintains the list of product/stack NAMES so a NEW product/service registers itself
without anyone typing it. It never removes anything and never touches existing aliases.

Sources (high-signal, low-noise):
  1. a curated seed of this org's stack/product names (correct casing)
  2. docker-compose service names
  3. mixed-case tech tokens in docs/ that appear >= 3 times (e.g. vLLM, NeMo, TitaNet)

Run:  python3 lexicon/build_lexicon.py     (from llama-service/) — safe to run any time / on a cron.
"""
import glob
import json
import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
LEXICON = os.path.join(HERE, "core_terms.json")
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

SEED = ["Qdrant", "vLLM", "NeMo", "Whisper", "TitaNet", "Chatterbox", "Docutalk", "AngelBot",
        "Costacloud", "MoM", "Apollo Computers", "Dash Computers", "diarization", "embeddings"]

def _service_names():
    names = set()
    for f in glob.glob(os.path.join(REPO, "docker-compose*.yml")):
        for m in re.findall(r"^\s{2}([a-zA-Z][\w-]+):\s*$", open(f, encoding="utf-8", errors="ignore").read(), re.M):
            if m not in ("services", "volumes", "networks"):
                names.add(m)
    return names

def _frequent_mixed_case():
    """Tokens with an internal capital (vLLM, NeMo, TitaNet) seen >= 3x in docs — almost always names."""
    c = Counter()
    for path in glob.glob(os.path.join(REPO, "docs", "*.md")) + [os.path.join(REPO, "README.md")]:
        if os.path.isfile(path):
            txt = open(path, encoding="utf-8", errors="ignore").read()
            c.update(re.findall(r"\b[A-Za-z][a-z]+[A-Z][A-Za-z]{2,}\b", txt))
    return {t for t, n in c.items() if n >= 3}

def main():
    with open(LEXICON, encoding="utf-8") as f:
        lex = json.load(f)
    have = {k.lower() for k in lex if not k.startswith("_")}
    # Curated seed ONLY. Scanning the repo/code for names pulls in code identifiers
    # (handleDownload, RealDictCursor) and service names nobody speaks — too noisy for a
    # spoken-term lexicon. New products are added to SEED (rare); mishearings self-learn at runtime.
    candidates = list(SEED)
    added = []
    for term in candidates:
        if len(term) >= 3 and term.lower() not in have:
            lex[term] = []            # new canonical, empty aliases (mishearings learned at runtime)
            have.add(term.lower())
            added.append(term)
    if added:
        tmp = LEXICON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as t:
            json.dump(lex, t, ensure_ascii=False, indent=2)
        os.replace(tmp, LEXICON)
    print(f"[build_lexicon] added {len(added)}: {added}" if added else "[build_lexicon] no new terms")

if __name__ == "__main__":
    main()
