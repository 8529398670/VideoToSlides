"""What this machine has to work with, and how much of it processing uses.

The Settings page shows the CPU and its core count, and a slider for how
many cores processing may keep busy (jobs.cpu_limit). worker_plan() turns
that limit into the scan and frame-grab worker counts each video gets: every
worker is one single-threaded ffmpeg process plus a little Python, so one
worker is roughly one busy core.

Inside a container the visible core count is the host's; a CPU quota
(docker run --cpus) is read from the cgroup so the slider tops out at what
the container may actually use.
"""
from __future__ import annotations

import math
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Config

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CPUINFO = Path("/proc/cpuinfo")
# How much less CPU time background processes get, on Unix (0 normal, 19 least).
LOW_PRIORITY_NICENESS = 10


@dataclass(frozen=True)
class Machine:
    cpu: str
    cores: int               # logical cores this process may use
    physical: int | None     # physical cores, where the OS says

    def to_public(self) -> dict:
        return {"cpu": self.cpu, "cores": self.cores, "physical": self.physical}


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _cpu_name() -> str:
    if platform.system() == "Darwin":
        name = _sysctl("machdep.cpu.brand_string")
        if name:
            return name
    try:
        for line in CPUINFO.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith(("model name", "hardware")):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown CPU"


def _quota_cores() -> int | None:
    """A cgroup v2 CPU quota, in whole cores ("max" means none)."""
    try:
        quota, period = CGROUP_CPU_MAX.read_text(encoding="utf-8").split()[:2]
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return max(1, math.ceil(int(quota) / int(period)))
    except (ValueError, ZeroDivisionError):
        return None


def logical_cores() -> int:
    counter = getattr(os, "process_cpu_count", None) or os.cpu_count
    cores = counter() or 1
    quota = _quota_cores()
    return max(1, min(cores, quota) if quota else cores)


def _physical_cores() -> int | None:
    if platform.system() == "Darwin":
        value = _sysctl("hw.physicalcpu")
        return int(value) if value and value.isdigit() else None
    try:
        text = CPUINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pairs, physical_id = set(), None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "physical id":
            physical_id = value.strip()
        elif key == "core id":
            pairs.add((physical_id, value.strip()))
    return len(pairs) or None


_detected: Machine | None = None


def detect() -> Machine:
    """Worked out once; the hardware does not change under a running server."""
    global _detected
    if _detected is None:
        _detected = Machine(cpu=_cpu_name(), cores=logical_cores(), physical=_physical_cores())
    return _detected


def worker_plan(cfg: Config) -> tuple[int, int]:
    """(scan workers, frame-grab workers) per video. With a CPU limit the
    limit is shared between the videos processed at once; without one, the
    configured counts are used as they are."""
    if cfg.jobs_cpu_limit <= 0:
        return cfg.jobs_scan_workers, cfg.jobs_extract_workers
    per_video = max(1, cfg.jobs_cpu_limit // max(1, cfg.jobs_max_concurrent))
    return min(cfg.jobs_scan_workers, per_video), min(cfg.jobs_extract_workers, per_video)


def lower_priority() -> None:
    """Make this process (and the ffmpeg processes it starts, which inherit
    it) yield to everything else on the machine. Best effort."""
    nice = getattr(os, "nice", None)
    if nice is None:
        return
    try:
        nice(LOW_PRIORITY_NICENESS)
    except OSError:
        pass
