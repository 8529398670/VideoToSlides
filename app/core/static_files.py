"""This project's one opinionated serving rule: markup and code are never
cached, images are cached hard.

Text assets (.html/.css/.js/...) are read and gzipped on every request with
Cache-Control: no-store. A deploy is therefore live on the next refresh, with
no content hashing, no ?v=123 query strings, and no cache-busting build step
to keep in sync. Gzip is what makes that affordable -- the bytes on the wire
are a fraction of the file.

Images and other binaries get the opposite treatment. Gzip does nothing for
already-compressed formats, and nobody wants a photo re-downloaded on every
page view, so those get a long max-age.

That trade-off is the whole reason this module exists instead of Starlette's
StaticFiles.
"""
from __future__ import annotations

import gzip
import mimetypes
import posixpath
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

# Extensions that are text, change often, and must never go stale.
ALWAYS_FRESH_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".txt", ".map",
    ".xml", ".webmanifest", ".yaml", ".yml",
}

BINARY_CACHE_SECONDS = 60 * 60 * 24 * 7  # 7 days

# The platform's MIME table varies; these are the ones the frontend relies on.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".map")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("application/yaml", ".yaml")
mimetypes.add_type("application/yaml", ".yml")


def has_dot_dot(request_path: str) -> bool:
    """Spot a parent-directory segment in the raw request.

    The resolved-path check in resolve() is the real guarantee -- nothing can
    escape the root without it. This exists so such a request answers 404
    directly instead of falling through to the index.html fallback, which is
    clearer to whoever reads the access log, and so the intent is stated
    rather than left implicit."""
    return ".." in request_path.replace("\\", "/").split("/")


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class StaticServer:
    def __init__(self, root: Path, index_file: str = "index.html") -> None:
        self.root = root.resolve()
        self.index_file = index_file

    def resolve(self, request_path: str) -> Path | None:
        """Turn a request path into a file path under the root, or None. The
        final check verifies where the path actually landed (after symlinks)
        rather than trying to blocklist how it got there.

        Every file this server sends comes through here, the index.html
        fallback included -- a path built by hand and served without this
        check is how a symlinked static/index.html pointing at ../config.yaml
        would reach a visitor. Static content should still be writable only by
        whoever deploys the app: the check runs before the read, so a writer
        able to swap a file for a symlink between the two could still win."""
        if has_dot_dot(request_path):
            return None
        cleaned = posixpath.normpath("/" + request_path.replace("\\", "/")).lstrip("/")
        if cleaned in ("", "."):
            return None
        candidate = (self.root / cleaned).resolve()
        if not candidate.is_relative_to(self.root):
            return None
        return candidate

    def _read_text_asset(self, path: Path, accepts_gzip: bool) -> tuple[bytes, bool]:
        body = path.read_bytes()
        if not accepts_gzip:
            return body, False
        # Fastest level: this runs on every request, and most of the saving
        # comes from the first pass anyway.
        return gzip.compress(body, compresslevel=1), True

    async def endpoint(self, request: Request) -> Response:
        """The catch-all. Registered last: it matches everything, so any route
        declared after it is unreachable."""
        request_path = request.path_params.get("path", "")
        if request_path == "" or request_path.endswith("/"):
            request_path = posixpath.join(request_path, self.index_file)

        path = self.resolve(request_path)
        if path is None:
            return PlainTextResponse("Not found", status_code=404)

        if not path.is_file():
            # A path with no extension is a client-side route ("/settings"),
            # so hand back index.html and let the browser router take over. A
            # path that names a file and is missing is a genuine 404 --
            # answering that with HTML would leave the browser parsing a page
            # as a stylesheet and reporting a confusing MIME error instead of
            # the real problem.
            if path.suffix != "":
                return PlainTextResponse("Not found", status_code=404)
            path = self.resolve(self.index_file)
            if path is None or not path.is_file():
                return PlainTextResponse("Not found", status_code=404)

        return await self._send(request, path)

    async def serve_login_page(self, request: Request) -> Response:
        """A login link opens the ordinary app page, which sees the /login/
        path and asks the person to confirm -- nothing is redeemed by the GET.
        See app/routes/auth.py."""
        path = self.resolve(self.index_file)
        if path is None or not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        response = await self._send(request, path)
        # The credential is in this page's URL; make sure no request the page
        # makes carries it onward in a Referer header.
        response.headers["referrer-policy"] = "no-referrer"
        return response

    async def _send(self, request: Request, path: Path) -> Response:
        """Serve a file resolve() has already approved, by the caching rule at
        the top of this module."""
        if path.suffix.lower() not in ALWAYS_FRESH_EXTENSIONS:
            return FileResponse(
                path,
                headers={"cache-control": f"public, max-age={BINARY_CACHE_SECONDS}", "vary": "Accept-Encoding"},
            )

        accepts_gzip = "gzip" in request.headers.get("accept-encoding", "")
        try:
            body, compressed = await run_in_threadpool(self._read_text_asset, path, accepts_gzip)
        except OSError:
            return PlainTextResponse("Not found", status_code=404)
        headers = {"cache-control": "no-store, no-cache, must-revalidate", "vary": "Accept-Encoding"}
        if compressed:
            headers["content-encoding"] = "gzip"
        return Response(body, media_type=content_type(path), headers=headers)
