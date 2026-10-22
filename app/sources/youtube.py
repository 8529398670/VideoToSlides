"""YouTube videos, playlists and channel tabs, through yt-dlp.

Only YouTube hosts are accepted. yt-dlp has a generic extractor that will
fetch any URL it is given, and a server that fetches arbitrary URLs on a
signed-in user's say-so can be pointed at its own internal network -- the
allowlist is what keeps this a YouTube feature rather than a proxy.

Downloads are video-only: slides need pixels, not sound, and skipping the
audio stream also skips the merge step that would otherwise need extra
scratch space.

yt-dlp needs a JavaScript runtime for YouTube's player code; deno and node
are both enabled and whichever is installed is used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
URL_MAX = 2048
JS_RUNTIMES = {"deno": {}, "node": {}}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PLACEHOLDER_TITLES = {"[Private video]", "[Deleted video]"}


class YouTubeError(Exception):
    """A URL that cannot be used, or a download that failed. Safe to show."""


@dataclass
class Playlist:
    """Where a batch of videos came from: a playlist, or a channel tab."""
    title: str
    url: str
    # The list= id; None for a channel tab, which has no playlist links.
    list_id: str | None


@dataclass
class Entry:
    url: str
    title: str
    # 1-based position in the playlist, counting videos that were skipped.
    index: int | None = None
    video_id: str | None = None

    def playlist_info(self, playlist: Playlist | None) -> dict | None:
        """What a job keeps about its playlist, for the deck's notes."""
        if playlist is None:
            return None
        video_url = None
        if playlist.list_id and self.video_id:
            query = {"v": self.video_id, "list": playlist.list_id}
            if self.index:
                query["index"] = str(self.index)
            video_url = "https://www.youtube.com/watch?" + urlencode(query)
        return {"title": playlist.title, "url": playlist.url, "index": self.index, "video_url": video_url}


def normalize_url(raw: object) -> str | None:
    """The URL if it points at YouTube over http(s), otherwise None."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or len(url) > URL_MAX:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        return None
    if parts.username or parts.password or parts.port:
        return None
    return url


def video_id(url: str | None) -> str | None:
    """The v= id of a watch URL, or the path of a youtu.be one."""
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() == "youtu.be":
        return parts.path.strip("/") or None
    return (parse_qs(parts.query).get("v") or [None])[0]


def _options(**extra: object) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # The container's filesystem is read-only outside the data dir, and a
        # cache of YouTube player code is not worth a writable path.
        "cachedir": False,
        "js_runtimes": JS_RUNTIMES,
        "socket_timeout": 30,
        **extra,
    }


def _message(error: Exception) -> str:
    text = ANSI.sub("", str(error)).replace("ERROR: ", "").strip()
    return text.splitlines()[0][:300] if text else "YouTube download failed"


def expand(url: str, max_items: int) -> tuple[Playlist | None, list[Entry]]:
    """(the playlist or None, the videos). A plain video URL is one entry
    -- even one carrying &list=, which is a video someone was watching inside
    a playlist, not a request for the playlist. A /playlist?list= URL, or a
    channel tab like /@name/videos, expands to its videos (flat: titles and
    URLs only, nothing downloaded)."""
    import yt_dlp  # heavy; only the processes that need it pay for it

    options = _options(extract_flat="in_playlist", skip_download=True, noplaylist=True, playlistend=max_items)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as error:  # noqa: BLE001 -- yt-dlp raises many types; all mean "cannot use this"
        raise YouTubeError(_message(error)) from None
    if not info:
        raise YouTubeError("nothing found at that URL")

    if info.get("_type") != "playlist":
        return None, [Entry(url=info.get("webpage_url") or url, title=info.get("title") or url)]

    entries: list[Entry] = []
    for index, entry in enumerate(info.get("entries") or [], 1):
        if not entry or entry.get("title") in PLACEHOLDER_TITLES:
            continue
        # A channel's top level lists its tabs (Videos, Shorts, ...), which
        # are playlists rather than videos; only videos become jobs.
        if entry.get("ie_key") not in (None, "Youtube"):
            continue
        video_id = entry.get("id")
        entry_url = entry.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
        if entry_url:
            entries.append(Entry(url=entry_url, title=entry.get("title") or entry_url, index=index, video_id=video_id))
        if len(entries) >= max_items:
            break
    if not entries:
        raise YouTubeError("no videos found in that playlist or channel (for a channel, use its /videos page)")
    page = info.get("webpage_url") or url
    list_id = (parse_qs(urlsplit(page).query).get("list") or [None])[0]
    return Playlist(title=info.get("title") or page, url=page, list_id=list_id), entries


def download(url: str, target_dir: Path, tmp_dir: Path, max_height: int,
             on_progress: Callable[[float], None] | None = None) -> tuple[Path, str]:
    """Download one video into target_dir as source.<ext>. Returns (path,
    title)."""
    import yt_dlp

    def hook(status: dict) -> None:
        if on_progress is None or status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        if total:
            on_progress(min(1.0, (status.get("downloaded_bytes") or 0) / total))

    height = int(max_height)
    options = _options(
        format=f"bv*[height<={height}][ext=mp4]/bv*[height<={height}]/b[height<={height}]/bv*/b",
        noplaylist=True,
        paths={"home": str(target_dir), "temp": str(tmp_dir)},
        outtmpl={"default": "source.%(ext)s"},
        progress_hooks=[hook],
        retries=3,
        fragment_retries=3,
        overwrites=True,
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as error:  # noqa: BLE001
        raise YouTubeError(_message(error)) from None

    downloads = (info or {}).get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else None
    if path is None or not path.is_file():
        raise YouTubeError("the download finished but produced no file")
    return path, (info or {}).get("title") or url
