"""From raw candidate cuts to the timestamps worth capturing: drop talking-head
noise, collapse transition bursts, and pick each slide's capture moment.
Ported from the original CLI; each function returns its result and leaves
reporting to the caller. Two additions over the original, both found with a
synthetic lecture: the opening slide (add_opening_slide) and the slide right
after a noise run (RUN_EXIT_QUIET_SEC) were both silently lost before."""
from __future__ import annotations

import bisect

from app.pipeline.scan import Candidate
from app.pipeline import tunables as T


def suppress_noise_runs(candidates: list[Candidate]) -> list[Candidate]:
    """Drop runs of candidates that look like talking-head/dead-air camera
    noise rather than real slide changes, using local candidate density
    (see the NOISE_* parameters in tunables.py for rationale and calibration)."""
    if not candidates:
        return candidates

    n = len(candidates)
    starts = [c["start_sec"] for c in candidates]

    def local_density(i: int) -> int:
        t = starts[i]
        lo, hi = t - T.NOISE_WINDOW_SEC / 2, t + T.NOISE_WINDOW_SEC / 2
        return bisect.bisect_right(starts, hi) - bisect.bisect_left(starts, lo)

    is_noisy = [local_density(i) >= T.NOISE_MIN_RATE for i in range(n)]

    # Group consecutive noisy candidates into raw runs.
    raw_runs = []
    i = 0
    while i < n:
        if is_noisy[i]:
            j = i
            while j < n and is_noisy[j]:
                j += 1
            raw_runs.append([i, j])
            i = j
        else:
            i += 1

    # Bridge runs separated by a short quiet gap (e.g. presenter briefly
    # stepping out of frame during an otherwise noisy dead-air stretch).
    merged_runs: list[list[int]] = []
    for run in raw_runs:
        if merged_runs and starts[run[0]] - starts[merged_runs[-1][1] - 1] <= T.NOISE_MERGE_GAP_SEC:
            merged_runs[-1][1] = run[1]
        else:
            merged_runs.append(run)

    drop_idx: set[int] = set()
    for a, b in merged_runs:
        duration = starts[b - 1] - starts[a]
        if duration >= T.NOISE_MIN_RUN_DURATION_SEC and (b - a) >= T.NOISE_MIN_RUN_LEN:
            drop_idx.update(range(a, b))
            # A run that ends in a long quiet stretch ended on a slide; its
            # last cut is that slide's start (see RUN_EXIT_QUIET_SEC).
            quiet_after = (starts[b] if b < n else float("inf")) - starts[b - 1]
            if quiet_after >= T.RUN_EXIT_QUIET_SEC:
                drop_idx.discard(b - 1)

    return [candidates[i] for i in range(n) if i not in drop_idx]


def merge_cut_bursts(scenes: list[Candidate]) -> list[Candidate]:
    """Collapse bursts of kept cuts closer than MIN_SLIDE_SEC into their LAST
    cut. A multi-step transition (dissolve, wipe, typewriter text build)
    fires several detector cuts within a second or two; treating each as a
    separate slide produced mid-render ghost captures, because those
    'slides' live shorter than the capture offsets and the midpoint
    fallback then lands INSIDE the animation. The last cut of a burst is
    the moment the new slide has settled -- the only state worth keeping.
    (The slide shown BEFORE the burst is unaffected: its capture time is
    computed against the burst's first raw cut, i.e. before the transition
    began.)"""
    merged: list[Candidate] = []
    for c in scenes:
        if merged and (c["start_sec"] - merged[-1]["start_sec"]) < T.MIN_SLIDE_SEC:
            merged[-1] = c   # burst continues: slide forward to the settled cut
        else:
            merged.append(c)
    return merged


def add_opening_slide(scenes: list[Candidate], all_cuts: list[Candidate]) -> list[Candidate]:
    """Slides are born at detected cuts, so whatever is on screen from 0s to
    the first cut -- usually the title slide -- would never be captured (the
    original CLI dropped it). Add it as a scene at 0s, but only when that
    opening stretch was quiet: no candidates at all, or a first raw cut that
    survived noise suppression. A talking-head intro fires candidates
    continuously, gets suppressed, and so gets no opening slide."""
    if not all_cuts:
        return [{"start_sec": 0.0, "energy": 0.0}]
    first_cut = all_cuts[0]["start_sec"]
    if first_cut < T.MIN_SLIDE_SEC or not scenes or scenes[0]["start_sec"] != first_cut:
        return scenes
    return [{"start_sec": 0.0, "energy": 0.0}] + scenes


def compute_capture_times(scenes: list[Candidate], all_cuts: list[Candidate], duration_sec: float) -> list[float]:
    """For each kept slide-cut, pick the timestamp to grab its frame at:
    CAPTURE_PRE_CUT_SEC before the NEXT detected change (or video end).
    By that moment the presenter has revealed every incremental
    animation/bullet build, so slides that populate piece-by-piece are
    captured complete instead of half-empty.

    'Next change' is looked up in the FULL pre-suppression cut list
    (all_cuts), not just the kept scenes: if a slide is followed by a
    dropped talking-head/dead-air run, we capture just before the camera
    cut -- while the slide is still on screen -- not one second before some
    much later kept cut (which would capture the talking head instead).

    If a slide is on screen too briefly to be both CAPTURE_MIN_AFTER_CUT_SEC
    after its start and CAPTURE_PRE_CUT_SEC before its end, the midpoint of
    its lifetime is used."""
    cut_times = [c["start_sec"] for c in all_cuts]
    times = []
    for s in scenes:
        t0 = s["start_sec"]
        i = bisect.bisect_right(cut_times, t0)
        nxt = cut_times[i] if i < len(cut_times) else duration_sec
        ts = nxt - T.CAPTURE_PRE_CUT_SEC
        if ts < t0 + T.CAPTURE_MIN_AFTER_CUT_SEC:
            ts = t0 + (nxt - t0) / 2.0
        times.append(min(max(ts, t0 + 0.1), duration_sec - 0.1))
    return times
