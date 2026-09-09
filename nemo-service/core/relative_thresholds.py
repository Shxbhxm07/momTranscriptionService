"""
Self-calibrating speaker-cluster decisions.

Replaces the fixed cosine constants in speaker_identifier (drop_noise_clusters' 0.15/0.35 bars and
the 0.92 merge) with logic derived from EACH meeting's OWN pairwise-cosine distribution. Pure numpy
— no torch/nemo — so it is unit-testable on synthetic distributions without the GPU.

Two decisions, both biased toward OVER-SPLITTING (an extra label is recoverable; a wrongly merged
speaker is gone — the "0.836 lesson", where two genuinely different colleagues scored 0.836 and a
fixed 0.80 merge fused them):

  • merge : over-split repair — one person emerging as >1 cluster. Decided from a NATURAL BREAK in
            this meeting's pairwise cosines PLUS a profile guardrail (the same voice relates to
            everyone else the same way). A conservative absolute floor stays ONLY as a backstop,
            because TitaNet different-speaker cosine was measured as high as ~0.836 here — the
            meeting's own distribution alone cannot always tell a similar-pair from a real split.
  • drop  : phantom/noise clusters — a cluster near-orthogonal to the real-speaker cohort. Decided
            from the GAP in this meeting's cohesion distribution AND a relative near-zero test
            (noise ~0; a distinct-but-real speaker sits at a healthy fraction of the cohort).

Order matters: MERGE first, then DROP on the merged survivors — otherwise a tight over-split inflates
the cohesion baseline and a genuinely-distinct speaker looks like noise. All inputs used by a
decision are recorded in `Decision.diagnostics` for instrumentation / A-B.
"""

from typing import Dict, List, Optional, Tuple
import numpy as np

# ── Safety rails — NOT per-meeting tuning ────────────────────────────────────────────────────────
# Measured, universal facts about TitaNet cosine on THIS pipeline, used only to keep the relative
# logic SAFE at the edges; the primary decisions are the per-meeting gap + profile.
MERGE_FLOOR      = 0.86  # never merge two clusters below this (0.836 different-speaker obs + margin)
MERGE_FLOOR_THIN = 0.90  # stricter floor with no 3rd cluster to profile against (2-cluster meetings)
MIN_MERGE_GAP    = 0.06  # a merge "natural break" must be at least this wide to be trusted
PROFILE_TOL      = 0.12  # same person => cosine to every OTHER cluster agrees within this
MIN_DROP_GAP     = 0.18  # a noise "natural break" in cohesion must be at least this wide
DROP_RATIO       = 0.35  # noise cohesion must be <= this fraction of the real cohort's cohesion
DROP_SUPPORT_MAX = 0.35  # never drop a cluster owning more than this fraction of talk-time


class Decision:
    """drop / merge_groups are in ORIGINAL labels so the caller applies them directly."""
    def __init__(self, drop: set, merge_groups: List[frozenset], diagnostics: dict):
        self.drop = drop                    # set[label] to remove as noise
        self.merge_groups = merge_groups    # list[frozenset[label]] each size >= 2 to fuse
        self.diagnostics = diagnostics

    def __repr__(self):
        return f"Decision(drop={sorted(self.drop)}, merge_groups={[sorted(g) for g in self.merge_groups]})"


def pairwise_cosine(cluster_embeddings: Dict[str, np.ndarray]) -> Tuple[List[str], np.ndarray]:
    """Build the NxN cosine matrix from L2-normalised cluster embeddings (dot == cosine)."""
    labels = list(cluster_embeddings.keys())
    n = len(labels)
    M = np.eye(n, dtype=float)
    for i in range(n):
        for j in range(i + 1, n):
            c = float(np.dot(cluster_embeddings[labels[i]], cluster_embeddings[labels[j]]))
            M[i, j] = M[j, i] = c
    return labels, M


def _largest_gap(values_desc: np.ndarray) -> Tuple[float, int]:
    """values sorted DESCENDING -> (gap_width, k) where values[:k+1] sit above the largest gap."""
    if len(values_desc) < 2:
        return 0.0, len(values_desc) - 1
    diffs = values_desc[:-1] - values_desc[1:]
    k = int(np.argmax(diffs))
    return float(diffs[k]), k


def _max_neighbor(M: np.ndarray) -> np.ndarray:
    """Per-cluster cohesion: its highest cosine to any OTHER cluster."""
    n = M.shape[0]
    return np.array([max((M[i, k] for k in range(n) if k != i), default=1.0) for i in range(n)])


def _same_profile(i: int, j: int, M: np.ndarray) -> bool:
    """Same person => cosine to EVERY other cluster agrees (within PROFILE_TOL). Relative — no
    absolute cosine. With no other cluster, defer to the absolute floor the caller already checked."""
    n = M.shape[0]
    others = [k for k in range(n) if k != i and k != j]
    if not others:
        return True
    a = np.array([M[i, k] for k in others])
    b = np.array([M[j, k] for k in others])
    return float(np.max(np.abs(a - b))) <= PROFILE_TOL


def _union_groups(labels: List[str], pairs: List[Tuple[int, int]]) -> List[frozenset]:
    parent = {i: i for i in range(len(labels))}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    groups: Dict[int, list] = {}
    for i in range(len(labels)):
        groups.setdefault(find(i), []).append(labels[i])
    return [frozenset(g) for g in groups.values() if len(g) > 1]


def _decide_merges(labels: List[str], M: np.ndarray, diag: dict) -> List[frozenset]:
    n = len(labels)
    if n < 2:
        return []
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    cos = np.array([M[i, j] for i, j in pairs])

    if len(pairs) == 1:  # thin data: no distribution — merge only a near-identical pair
        i, j = pairs[0]
        merge = bool(cos[0] >= MERGE_FLOOR_THIN)
        diag["merge"] = {"mode": "thin", "cos": round(float(cos[0]), 3), "floor": MERGE_FLOOR_THIN, "merge": merge}
        return _union_groups(labels, [(i, j)] if merge else [])

    order = np.argsort(cos)[::-1]
    scos = cos[order]
    gap, k = _largest_gap(scos)
    boundary = float(scos[k])  # pairs with cos >= boundary sit above the meeting's natural break
    diag["merge"] = {"mode": "gap", "boundary": round(boundary, 3), "gap": round(gap, 3),
                     "floor": MERGE_FLOOR, "min_gap": MIN_MERGE_GAP, "pairs": []}

    merged: List[Tuple[int, int]] = []
    if gap >= MIN_MERGE_GAP and boundary >= MERGE_FLOOR:
        for idx, (i, j) in enumerate(pairs):
            c = float(cos[idx])
            if c < boundary:
                continue
            prof = _same_profile(i, j, M)
            ok = c >= MERGE_FLOOR and prof
            diag["merge"]["pairs"].append({"pair": (labels[i], labels[j]), "cos": round(c, 3),
                                           "profile_ok": prof, "merge": ok})
            if ok:
                merged.append((i, j))
    return _union_groups(labels, merged)


def _reduce(labels: List[str], M: np.ndarray, merge_groups: List[frozenset]):
    """Collapse each merge group into one cluster; its cosine to another cluster is the mean over the
    member cross-pairs (over-split members are ~the same voice, so this is a faithful approximation)."""
    rep = {l: l for l in labels}
    for g in merge_groups:
        r = sorted(g)[0]
        for l in g:
            rep[l] = r
    new_labels, seen = [], set()
    for l in labels:
        if rep[l] not in seen:
            seen.add(rep[l]); new_labels.append(rep[l])
    grp = {r: [l for l in labels if rep[l] == r] for r in new_labels}
    idx = {l: i for i, l in enumerate(labels)}
    n = len(new_labels)
    NM = np.eye(n)
    for a in range(n):
        for b in range(a + 1, n):
            ma = [idx[l] for l in grp[new_labels[a]]]
            mb = [idx[l] for l in grp[new_labels[b]]]
            c = float(np.mean([M[i, j] for i in ma for j in mb]))
            NM[a, b] = NM[b, a] = c
    return new_labels, NM, rep


def _decide_drops(labels: List[str], M: np.ndarray, support: Optional[Dict[str, float]], diag: dict) -> set:
    n = len(labels)
    if n < 3:  # no cohort to be an outlier from — prefer-keep (a 2nd cluster is likely a real speaker)
        diag["drop"] = {"mode": "too_few", "n": n}
        return set()

    coh = _max_neighbor(M)
    order = np.argsort(coh)             # ascending: least-cohesive (noise) first
    scoh = coh[order]
    gaps = scoh[1:] - scoh[:-1]
    k = int(np.argmax(gaps))
    gap = float(gaps[k])
    low_idx = [int(order[p]) for p in range(k + 1)]
    high_idx = [int(order[p]) for p in range(k + 1, n)]
    cohort = float(np.median(coh[high_idx])) if high_idx else 1.0
    low_max = float(np.max(coh[low_idx]))
    diag["drop"] = {"mode": "gap", "cohesion": {labels[i]: round(float(coh[i]), 3) for i in range(n)},
                    "gap": round(gap, 3), "cohort": round(cohort, 3), "low_max": round(low_max, 3),
                    "min_gap": MIN_DROP_GAP, "ratio": DROP_RATIO}

    drops: set = set()
    # Noise iff a REAL separation (gap) AND the low group is near-orthogonal RELATIVE to the cohort
    # (a distinct-but-real speaker sits at a healthy fraction of the cohort; noise ~ 0).
    if gap >= MIN_DROP_GAP and (k + 1) < n and (cohort <= 0 or low_max <= DROP_RATIO * cohort):
        for i in low_idx:
            lab = labels[i]
            frac = None if support is None else float(support.get(lab, 0.0))
            if frac is None or frac <= DROP_SUPPORT_MAX:
                drops.add(lab)
    diag["drop"]["dropped"] = sorted(drops)
    return drops if len(drops) < n else set()  # never drop everything


def decide(labels: List[str], M: np.ndarray, support: Optional[Dict[str, float]] = None) -> Decision:
    """Per-meeting decision from the cluster cosine matrix `M` (NxN symmetric, diag=1) and optional
    talk-time `support` ({label: fraction}). Returns clusters to drop (noise) and groups to merge
    (over-split). MERGE is decided first, then DROP on the merged survivors."""
    diag: dict = {"n": len(labels)}
    merge_groups = _decide_merges(labels, M, diag)

    new_labels, NM, rep = _reduce(labels, M, merge_groups)
    new_support = None
    if support is not None:
        new_support = {}
        for l in labels:
            new_support[rep[l]] = new_support.get(rep[l], 0.0) + float(support.get(l, 0.0))

    drop_reduced = _decide_drops(new_labels, NM, new_support, diag)
    drop = {l for l in labels if rep[l] in drop_reduced}
    return Decision(drop=drop, merge_groups=merge_groups, diagnostics=diag)


def _nearest_by_time(seg: dict, candidates: List[dict]) -> str:
    """Reassign a dropped (noise) segment to the nearest real-speaker segment: most time overlap,
    else nearest midpoint. Mirrors speaker_identifier._nearest_by_time so behaviour is unchanged."""
    if not candidates:
        return seg["speaker"]
    best, best_ov = None, 0.0
    for c in candidates:
        ov = max(0.0, min(seg["end"], c["end"]) - max(seg["start"], c["start"]))
        if ov > best_ov:
            best_ov, best = ov, c["speaker"]
    if best is not None:
        return best
    mid = (seg["start"] + seg["end"]) / 2
    return min(candidates, key=lambda c: abs((c["start"] + c["end"]) / 2 - mid))["speaker"]


def apply_decision(segments: List[dict], cluster_embeddings: Dict[str, np.ndarray], decision: "Decision"):
    """Apply a Decision to the diarization output. Pure (numpy + segment dicts only), so it is
    unit-testable without torch/nemo. Returns (new_segments, new_cluster_embeddings).

    MERGE: relabel each group's members to the group's representative and re-average+renormalise
    their embeddings (identify then uses the whole-person voiceprint). DROP: reassign each noise
    segment to the nearest surviving real speaker by time, and remove the noise embedding.
    """
    from collections import defaultdict

    labels = list(cluster_embeddings.keys())

    # 1) MERGE — relabel members to the representative, re-average embeddings
    remap = {l: l for l in labels}
    for group in decision.merge_groups:
        root = sorted(group)[0]
        for l in group:
            remap[l] = root
    segments = [dict(s, speaker=remap[s["speaker"]]) for s in segments]

    groups: Dict[str, list] = defaultdict(list)
    for l in labels:
        groups[remap[l]].append(cluster_embeddings[l])
    embs: Dict[str, np.ndarray] = {}
    for root, es in groups.items():
        avg = np.mean(es, axis=0)
        nrm = float(np.linalg.norm(avg))
        embs[root] = avg / nrm if nrm > 0 else avg

    # 2) DROP — reassign noise segments to the nearest surviving speaker by time
    drop = set(decision.drop)
    real = [l for l in embs if l not in drop]
    if drop and real:
        real_segs = [s for s in segments if s["speaker"] in real]
        segments = [
            (dict(s, speaker=_nearest_by_time(s, real_segs)) if s["speaker"] in drop else s)
            for s in segments
        ]
        embs = {l: e for l, e in embs.items() if l not in drop}

    return segments, embs
