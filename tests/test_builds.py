"""Build-step detection (app/pipeline/builds.py) on synthetic slides.

Run from the project root:  ./.venv/bin/python -m unittest discover tests

The slides imitate a screen-recorded lecture: a title, bullet lines that
appear one at a time, a webcam pane in the corner whose picture changes in
every capture, and a mouse cursor that wanders. The calibration on a real
video (YouTube r95bMQF4Uj0: 19 captures -> 8 slides) is recorded in
app/pipeline/tunables.py; these tests pin the behaviour that must not drift.
"""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.pipeline import builds

W, H = 1280, 720
CAM = (20, 520, 240, 700)  # webcam pane box, bottom-left
FONT = ImageFont.load_default(size=34)
TITLE_FONT = ImageFont.load_default(size=48)


def slide(title: str, bullets: list[str], *, seed: int, dark: bool = False, cursor: tuple[int, int] | None = None,
          photo: bool = False) -> Image.Image:
    rng = random.Random(seed)
    bg, fg = ((25, 28, 34), (235, 235, 235)) if dark else ((255, 255, 255), (20, 20, 20))
    if photo:
        noise = np.random.default_rng(seed).integers(0, 255, (H // 8, W // 8, 3), dtype=np.uint8)
        image = Image.fromarray(noise).resize((W, H), Image.BILINEAR)
    else:
        image = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(image)
    draw.text((80, 60), title, fill=(40, 40, 160) if not dark else (140, 170, 255), font=TITLE_FONT)
    for i, line in enumerate(bullets):
        draw.text((110, 170 + i * 70), "- " + line, fill=fg, font=FONT)
    # The webcam: a different "picture" every time.
    cam = np.random.default_rng(seed + 1000).integers(0, 255, ((CAM[3] - CAM[1]) // 10, (CAM[2] - CAM[0]) // 10, 3),
                                                      dtype=np.uint8)
    image.paste(Image.fromarray(cam).resize((CAM[2] - CAM[0], CAM[3] - CAM[1]), Image.NEAREST), CAM[:2])
    if cursor is None:
        cursor = (rng.randint(700, 1200), rng.randint(450, 650))
    x, y = cursor
    draw.polygon([(x, y), (x, y + 22), (x + 6, y + 16), (x + 14, y + 16)], fill=(0, 0, 0), outline=(255, 255, 255))
    return image


def webcam_motion() -> np.ndarray:
    """What the scan would report: the webcam pane moves in every sample."""
    motion = np.zeros((90, 160), dtype=np.float32)
    motion[CAM[1] * 90 // H: CAM[3] * 90 // H + 1, CAM[0] * 160 // W: CAM[2] * 160 // W + 1] = 0.9
    return motion


class BuildStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def flags(self, images: list[Image.Image], motion: np.ndarray | None = None) -> list[bool]:
        paths = []
        for i, image in enumerate(images):
            path = self.root / f"s{i:03d}.jpg"
            image.save(path, "JPEG", quality=88)
            paths.append(path)
        flags, _ = builds.find_build_steps(paths, motion if motion is not None else webcam_motion())
        return flags

    def test_bullet_build_chain_keeps_only_the_last(self) -> None:
        bullets = ["Non-negativity: p(x) >= 0", "Normalization: integral of p = 1", "Probability of a box"]
        steps = [slide("Properties", bullets[:n], seed=n) for n in (1, 2, 3)]
        self.assertEqual(self.flags(steps), [True, True, False])

    def test_new_slide_with_same_layout_is_kept(self) -> None:
        a = slide("Discrete random variables", ["A countable image"], seed=1)
        b = slide("Continuous random variables", ["An uncountable image"], seed=2)
        self.assertEqual(self.flags([a, b]), [False, False])

    def test_slide_that_loses_content_is_kept(self) -> None:
        a = slide("Agenda", ["One", "Two", "Three"], seed=1)
        b = slide("Agenda", ["One"], seed=2)
        self.assertEqual(self.flags([a, b]), [False, False])

    def test_replaced_bullet_is_not_a_build(self) -> None:
        a = slide("Summary", ["First point here", "Second point here"], seed=1)
        b = slide("Summary", ["First point here", "Something else entirely", "Third point"], seed=2)
        self.assertEqual(self.flags([a, b]), [False, False])

    def test_dark_theme_build(self) -> None:
        a = slide("Dark deck", ["Alpha"], seed=1, dark=True)
        b = slide("Dark deck", ["Alpha", "Beta and gamma"], seed=2, dark=True)
        self.assertEqual(self.flags([a, b]), [True, False])

    def test_photo_slides_are_left_alone(self) -> None:
        a = slide("Photo", [], seed=1, photo=True)
        b = slide("Photo", ["Caption added"], seed=2, photo=True)
        self.assertEqual(self.flags([a, b]), [False, False])

    def test_without_a_motion_map_a_live_webcam_blocks_merging(self) -> None:
        # No scan data (a job made before motion maps): nothing is ignored, so
        # the changing webcam pane reads as changed content. Missing a merge is
        # the safe failure; a wrong merge would drop a real slide.
        steps = [slide("Properties", ["One", "One more line here"][:n], seed=n) for n in (1, 2)]
        paths = []
        for i, image in enumerate(steps):
            path = self.root / f"n{i}.jpg"
            image.save(path, "JPEG", quality=88)
            paths.append(path)
        flags, _ = builds.find_build_steps(paths, None)
        self.assertEqual(flags, [False, False])

    def test_alternating_slides_are_not_merged(self) -> None:
        # A presenter flipping back and forth between two slides.
        a = slide("Market supply", ["Price rises", "Quantity supplied rises"], seed=1)
        b = slide("Market demand", ["Price rises", "Quantity demanded falls"], seed=2)
        self.assertEqual(self.flags([a, b, a, b]), [False, False, False, False])

if __name__ == "__main__":
    unittest.main()
