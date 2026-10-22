"""Every number that controls detection sensitivity.

Defaults were calibrated on real 30 fps lecture recordings with a corner
webcam pane. These are algorithm calibration rather than deployment settings,
which is why they live here and not in config.yaml: changing one changes
which slides come out, and that wants a test video, not an ops edit.

How it works (and why it is fast):

1. Coarse-to-fine detection: instead of analyzing every frame, the scanner
   samples every STRIDE-th frame with a cheap downscaled grayscale pixel
   diff. This is safe because slides never change faster than about once
   per second.
2. Parallel scan: the video is split into N time-chunks and each chunk is
   decoded by its own ffmpeg process, so every core is busy.
3. Parallel frame extraction: pulling the confirmed capture frames is one
   ffmpeg call each, run from a thread pool.
4. Density-based noise suppression: talking-head / dead-air segments fire
   candidates almost continuously; genuine slide changes are tens of seconds
   to minutes apart. Dense runs of candidates are dropped as noise.
5. Full-build capture: each slide's frame is grabbed just BEFORE the next
   detected change, when every animation/bullet build has been revealed.
6. Loose duplicate filtering: only near-pixel-identical repeats are merged.
"""

# --- Accepted inputs ---------------------------------------------------------
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm")

# --- Coarse scan -------------------------------------------------------------
STRIDE = 5                   # analyze every Nth frame. Lower = more sensitive
                             # to very brief flashes, but proportionally slower.
SCAN_WIDTH = 160             # frames are compared at this size (area-averaged),
SCAN_HEIGHT = 90             # which is what makes the diff cheap and noise-tolerant.
COARSE_DIFF_THRESH = 3.0     # mean abs luminance diff (0-255) between sampled
                             # frames to flag a slide change. Lower = more
                             # candidates (more sensitive, more false positives).
GRADUAL_WINDOW_SEC = 2.0     # also compare each sample with the one this far back,
                             # so a dissolve/fade transition is caught: a 1s fade
                             # spreads its change over ~6 samples, each one under
                             # COARSE_DIFF_THRESH, and the frame-to-frame test alone
                             # sees nothing. Only fires when no cut was found in the
                             # previous GRADUAL_WINDOW_SEC, so a hard cut is never
                             # counted twice (which would inflate the candidate
                             # density the noise suppression below relies on).
                             # Must stay well under the gap between bullet builds,
                             # or successive builds add up into a "change".
GRADUAL_CELL_THRESH = 8      # ...and a fade must be BROAD: the frame is split into
GRADUAL_MIN_AREA = 0.12      # 10x10-pixel cells (of the 160x90 scan frame), and at
                             # least this fraction of cells must have changed by
                             # more than GRADUAL_CELL_THRESH. Over 2s a moving
                             # webcam pane alone can push the mean diff past the
                             # threshold, but it covers ~4% of cells; one new
                             # bullet line ~8%; a real slide change 17-25%
                             # (measured on a synthetic lecture with a corner pane).
MIN_GAP_SEC = 0.5            # minimum spacing between reported candidate cuts.
MIN_SLIDE_SEC = 3.0          # kept cuts closer together than this are treated as
                             # ONE transition event (dissolve/wipe/animation burst)
                             # and collapsed into the burst's LAST cut. Prevents
                             # mid-render ghost captures; side effect: slides
                             # flipped through faster than this keep only the
                             # last one (they're unreadable in a lecture anyway).

# --- Capture timing ----------------------------------------------------------
# Each slide's frame is captured just BEFORE the NEXT detected change, not
# just after the slide appears. Rationale: slides that build up through
# animations (bullets appearing one by one) are only complete at the END of
# their time on screen -- by the moment the presenter moves on, everything
# has been revealed. Capturing early would save them half-empty.
CAPTURE_PRE_CUT_SEC = 1.0    # capture this long before the next detected change.
CAPTURE_MIN_AFTER_CUT_SEC = 0.5  # ...but never sooner than this after the slide
                             # first appears (lets the transition settle). If the
                             # slide is on screen too briefly to honor both, the
                             # midpoint of its lifetime is used.

# --- Talking-head / dead-air suppression -------------------------------------
# Static camera shots of a presenter (intro, Q&A) trigger the detector
# continuously via compression noise and small movements. Per-candidate
# energy does NOT separate these from real slide changes (ranges overlap);
# candidate DENSITY over time does: real slide gaps are tens of seconds to
# minutes, noise fires every ~0.5-20 s. Dense runs are merged (bridging
# short quiet gaps) and dropped if long enough.
NOISE_WINDOW_SEC = 20.0      # window centered on each candidate for density.
NOISE_MIN_RATE = 4           # candidates within the window to mark it "noisy".
                             # WARNING: 3 was tried and it silently deleted real
                             # case-presentation sequences (slides flipped every
                             # 6-10s look "dense"). Do not lower below 4.
NOISE_MERGE_GAP_SEC = 40.0   # bridge noisy stretches separated by less than this.
NOISE_MIN_RUN_DURATION_SEC = 8.0  # merged noisy run must span at least this long...
NOISE_MIN_RUN_LEN = 5        # ...and contain this many candidates to be dropped.
                             # Occasional full-frame speaker/audience shots may
                             # slip through during Q&A -- exclude those slides in
                             # the review grid; making this more aggressive costs
                             # real slides.
RUN_EXIT_QUIET_SEC = 10.0    # the LAST cut of a dropped noise run is kept as a
                             # slide when nothing changes for this long after it:
                             # a noisy stretch ending in a static picture is the
                             # camera cutting back to the slides, and that slide's
                             # birth cut is part of the run. Without this, the
                             # first slide after every Q&A/talking-head stretch
                             # was lost. Raise it if presenter shots that end in a
                             # long still frame slip through.

# --- Duplicate filtering -----------------------------------------------------
# A pair of consecutive frames is merged as a duplicate only when BOTH tests
# agree nothing changed (AND logic): the global perceptual hash and a
# spatially localized region diff. AND logic matters because a small moving
# webcam pane flips global-hash bits even when the slide is static, while
# the region diff correctly stays low (only one grid cell changes).
# A genuinely new slide blows past both thresholds by a wide margin
# (measured: hash_dist 95 and region_diff 0.52 vs thresholds 1 and 0.005).
HASH_SIZE = 16               # perceptual (dhash) resolution.
HASH_DIST_THRESH = 1         # hamming distance >= this counts as "changed".
GRID = 16                    # region-diff grid (GRID x GRID cells).
REGION_CELL_THRESH = 18      # per-cell mean luminance delta to count a cell changed.
REGION_MIN_DIFF = 0.005      # fraction of changed cells >= this counts as "changed".

# --- Incremental builds --------------------------------------------------------
# Presenters often reveal a slide piece by piece (bullets, then a chart). When
# each reveal is big enough to register as a change, every intermediate state
# gets captured as its own slide. A capture is a "build step" when the NEXT
# capture only ADDS content to it -- on the same background, with nothing
# removed or replaced. Build steps are excluded from the deck (still visible
# and re-includable in the review grid), leaving the completed slide.
# Calibrated on YouTube r95bMQF4Uj0 (Beamer pauses + corner webcam): 19
# captures -> 8 real slides.
BUILD_MERGE_ENABLED = True
MOTION_PIXEL_THRESH = 6      # a scan-frame pixel "moved" when its luma changes
                             # by more than this between samples.
MOTION_MIN_FRACTION = 0.15   # pixels that move in at least this fraction of all
                             # samples are live video (a webcam pane, a clock) and
                             # are ignored when comparing slides. Slide content
                             # changes in well under 1% of samples.
BUILD_WIDTH = 480            # slides are compared at this width.
BUILD_BG_TOLERANCE = 20      # background luma of the two captures must match
                             # within this, or it is a different slide.
BUILD_INK_THRESH = 45        # a pixel is content ("ink") when its luma differs
                             # from the background by more than this.
BUILD_CELL = 16              # comparison grid cell size, in pixels at BUILD_WIDTH.
BUILD_CELL_MIN_PIXELS = 6    # a cell counts as added/lost content with at least
                             # this many such pixels (absorbs JPEG speckle).
BUILD_MIN_ADDED_CELLS = 5    # the next capture must add at least this much...
BUILD_MAX_LOSS_CELLS = 3     # ...while changing at most this many cells of the
BUILD_MAX_LOSS_RATIO = 0.10  # existing content, or this fraction of what it adds
                             # if more. A moving mouse cursor costs a cell or
                             # two. Measured: build steps add 17-92 cells and
                             # lose 0-1; real slide changes lose 114-276, always
                             # more than they add.

# --- Output ------------------------------------------------------------------
SLIDE_WIDTH_IN = 13.333      # 16:9 PowerPoint width; height follows video aspect.
THUMB_WIDTH = 320            # review-grid thumbnail width in pixels.
JPEG_QUALITY = 2             # ffmpeg -q:v for extracted slides (2 = near-lossless).
