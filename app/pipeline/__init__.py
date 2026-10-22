"""Slide extraction: turn one lecture recording into a folder of slide images.

This is the detection pipeline from the original slides_extractor.py CLI,
split one stage per module and with the CLI parts (picker, argparse, tqdm)
removed. Nothing here knows about the web server, the database, or users --
runner.process_video takes a video path and an output directory and reports
progress through a callback, so the same code runs from the worker process,
a test, or a shell.

    probe.py      ffprobe: size, frame rate, frame count
    scan.py       coarse parallel pixel-diff scan -> candidate cuts
    detect.py     noise-run suppression, burst merging, capture timing
    extract.py    one ffmpeg grab per capture time, plus thumbnails
    dedupe.py     drop near-identical consecutive frames
    pptx_deck.py  assemble a .pptx from a list of slide images
    runner.py     the stages in order
    tunables.py   every detection threshold, with its rationale

Tuning: all thresholds live in tunables.py, each with a comment explaining
what it does and which way to move it.
"""
