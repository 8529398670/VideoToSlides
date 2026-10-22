"""What ffprobe says about a video: size, frame rate, frame count, length."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

PROBE_TIMEOUT_SECONDS = 60


class ProbeError(Exception):
    """The file is not a video ffprobe can read. The message is safe to show."""


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    nb_frames: int

    @property
    def duration_sec(self) -> float:
        return self.nb_frames / self.fps


def _rate(value: str | None) -> float:
    if not value or "/" not in value:
        return 0.0
    num, den = value.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0


def get_video_info(video_path: str) -> VideoInfo:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames:format=duration",
             "-of", "json", video_path],
            capture_output=True, text=True, check=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(probe.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        raise ProbeError("not a readable video file") from None

    streams = data.get("streams") or []
    if not streams:
        raise ProbeError("the file has no video stream")
    stream = streams[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    # r_frame_rate is the stream's nominal rate; avg_frame_rate covers the
    # odd file where that is 0/0.
    fps = _rate(stream.get("r_frame_rate")) or _rate(stream.get("avg_frame_rate"))
    if width <= 0 or height <= 0 or fps <= 0 or fps > 1000:
        raise ProbeError("could not read the video's size or frame rate")

    nb_frames = str(stream.get("nb_frames") or "")
    if nb_frames.isdigit() and int(nb_frames) > 0:
        frames = int(nb_frames)
    else:
        # Containers like mkv/webm do not store a frame count.
        try:
            duration = float((data.get("format") or {}).get("duration") or 0)
        except ValueError:
            duration = 0.0
        frames = int(duration * fps)
    if frames <= 0:
        raise ProbeError("could not read the video's length")
    return VideoInfo(width=width, height=height, fps=fps, nb_frames=frames)
