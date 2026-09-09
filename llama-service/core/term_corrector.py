"""
Tier-1 deterministic term correction (offline, stdlib only) + SELF-LEARNING.

Fixes an org's CORE recurring vocabulary — product/client names + key stack terms — that must be
exact and that the LLM guesses wrong. Runs BEFORE the LLM so the LLM can't turn a hard mishearing
into a *different* real product ("Vsperge"->"Vespa"). Bounded to OUR terms; general jargon is left
to the LLM (Tier 2).

SELF-LEARNING (fully automatic, no user): after the LLM corrects a transcript, `learn_from_correction`
diffs the LLM's input vs output. When the LLM mapped a garbled word to a term we ALREADY own (and it
sounds alike and isn't a real English word), that mishearing is appended to core_terms.json as a new
alias — so next time the fast deterministic layer catches it. The LLM is the teacher; the lexicon
gets smarter every meeting, invisibly.

Match mechanisms (most-confident first): multi-word alias, single-word alias, casing, conservative
phonetic (metaphone-lite + edit-sim, guarded by a common-English wordlist).
"""
import json
import logging
import os
import re
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class TermCorrector:
    PHON_THRESHOLD = 0.86       # apply: how close a token must sound to auto-correct it
    LEARN_THRESHOLD = 0.58      # learn: sanity floor so we only learn genuine mishearings (not grammar edits)
    MIN_PHON_LEN = 4
    MAX_ALIASES_PER_TERM = 40   # cap so a term never accumulates junk

    def __init__(self, lexicon_path: str, common_path: str = None):
        self.lexicon_path = lexicon_path
        self.common_path = common_path
        self._build()

    # ---- load / (re)build in-memory structures from the JSON ----
    def _build(self):
        with open(self.lexicon_path, encoding="utf-8") as f:
            lex = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        self.canon = list(lex)
        self.single = [c for c in self.canon if " " not in c]
        self.canon_map = {c.lower(): c for c in self.single}
        self.alias_1 = {a.lower(): c for c, al in lex.items() for a in al if " " not in a}
        self.alias_n = sorted(
            [(a.lower(), c) for c, al in lex.items() for a in al if " " in a],
            key=lambda x: -len(x[0]),
        )
        self.pkeys = {c: self._pkey(c) for c in self.single}
        self.common = self._load_common(self.common_path)
        logger.info("[TERMCORR] %d terms, %d aliases, %d common words",
                    len(self.canon), len(self.alias_1) + len(self.alias_n), len(self.common))

    @staticmethod
    def _pkey(word: str) -> str:
        w = re.sub(r"[^a-z]", "", word.lower())
        if not w:
            return ""
        for a, b in [("ph", "f"), ("gh", ""), ("ck", "k"), ("q", "k"), ("x", "ks"),
                     ("z", "s"), ("c", "k"), ("wh", "w"), ("w", ""), ("y", ""), ("h", "")]:
            w = w.replace(a, b)
        if not w:
            return ""
        return re.sub(r"(.)\1+", r"\1", w[0] + re.sub(r"[aeiou]", "", w[1:]))

    # A real English wordlist has ~100k entries. Anything close to the 293-word lexicon fallback
    # means the dictionary did not load, and the phonetic matcher below MUST NOT run without it —
    # see the note in the Dockerfile for the 102 real words it corrupts when the guard is missing.
    MIN_COMMON_WORDS = 10_000

    def _load_common(self, path: str) -> set:
        words: set = set()
        # /usr/share/dict/words is the Debian alternatives symlink installed by `wamerican`;
        # american-english is the concrete file, checked too in case only one is present.
        for p in ("/usr/share/dict/words", "/usr/share/dict/american-english", path):
            if p and os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        for ln in f:
                            words.update(w for w in ln.strip().lower().split() if w)
                except OSError:
                    pass
        if len(words) < self.MIN_COMMON_WORDS:
            # FAIL LOUD, never degrade quietly. The caller disables Tier 1 entirely on this error,
            # so the LLM (Tier 2) still corrects the transcript — output stays safe, just without
            # deterministic term fixes. The previous behaviour was to carry on with a 28-word list,
            # which silently turned the guard off while leaving the corrections switched on.
            raise RuntimeError(
                f"over-correction guard unusable: only {len(words)} common words loaded, need "
                f"{self.MIN_COMMON_WORDS:,}. Install the wordlist in the image "
                f"(apt-get install wamerican) — do NOT rely on a bind mount, it does not exist "
                f"outside docker-compose."
            )
        return words

    # ---- Tier 1: apply corrections ----
    def correct(self, text: str):
        """Return (corrected_text, [(from, to), ...])."""
        if not text:
            return text, []
        fixes = []
        for alias, canon in self.alias_n:                     # multi-word aliases first
            pat = re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)
            if pat.search(text):
                text = pat.sub(canon, text)
                fixes.append((alias, canon))
        toks = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
        for i, tok in enumerate(toks):
            if not tok[:1].isalpha():
                continue
            t = tok.lower()
            m = None
            if t in self.alias_1:
                m = self.alias_1[t]
            elif t in self.canon_map:
                m = self.canon_map[t] if tok != self.canon_map[t] else None
            elif len(t) >= self.MIN_PHON_LEN and t not in self.common:
                best, bs = None, 0.0
                for c in self.single:
                    s = max(SequenceMatcher(None, self._pkey(t), self.pkeys[c]).ratio(),
                            SequenceMatcher(None, t, c.lower()).ratio())
                    if s > bs:
                        bs, best = s, c
                if bs >= self.PHON_THRESHOLD:
                    m = best
            if m and m != tok:
                fixes.append((tok, m))
                toks[i] = m
        return re.sub(r"[ \t]{2,}", " ", "".join(toks)), fixes

    # ---- self-learning: the LLM teaches the lexicon ----
    def learn_from_correction(self, before: str, after: str) -> dict:
        """Diff LLM-input vs LLM-output; learn single-token mishearings the LLM fixed to a term we
        already own. Returns {canonical: [new_aliases]} (also persisted). Heavily guarded."""
        if not before or not after:
            return {}
        a = re.findall(r"[A-Za-z][A-Za-z']*", before)
        b = re.findall(r"[A-Za-z][A-Za-z']*", after)
        sm = SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b])
        learned: dict = {}
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace" or (i2 - i1) != 1 or (j2 - j1) != 1:
                continue                                       # only 1-token -> 1-token substitutions
            x, y = a[i1].lower(), b[j1].lower()
            if y not in self.canon_map:                        # target must be a term we OWN
                continue
            canon = self.canon_map[y]
            if x == y or x in self.alias_1 or x in self.canon_map:  # already known
                continue
            if len(x) < 3 or x in self.common:                 # protect real words / too short
                continue
            sim = max(SequenceMatcher(None, self._pkey(x), self.pkeys[canon]).ratio(),
                      SequenceMatcher(None, x, y).ratio())
            if sim < self.LEARN_THRESHOLD:                     # must SOUND alike (reject grammar edits)
                continue
            learned.setdefault(canon, set()).add(x)
        learned = {c: sorted(s) for c, s in learned.items() if s}
        if learned:
            self._persist_aliases(learned)
        return learned

    def _persist_aliases(self, mapping: dict):
        """Append learned aliases to core_terms.json (exclusive lock + atomic replace), then reload."""
        import fcntl
        try:
            with open(self.lexicon_path, "r+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                data = json.load(f)
                changed = False
                for canon, aliases in mapping.items():
                    cur = data.get(canon)
                    if not isinstance(cur, list):              # only extend existing terms, never invent
                        continue
                    existing = {al.lower() for al in cur}
                    for al in aliases:
                        if al not in existing and len(cur) < self.MAX_ALIASES_PER_TERM:
                            cur.append(al); existing.add(al); changed = True
                    data[canon] = cur
                if changed:
                    tmp = self.lexicon_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as t:
                        json.dump(data, t, ensure_ascii=False, indent=2)
                    os.replace(tmp, self.lexicon_path)
                fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            logger.warning("[TERMCORR] alias persist failed (non-critical): %s", e)
            return
        self._build()                                          # reload so the new aliases apply immediately
