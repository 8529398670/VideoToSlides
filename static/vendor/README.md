# vendor/

Third-party browser libraries live here as plain files, downloaded once and
served by this app.

Nothing in this project is compiled, bundled, or transpiled, and nothing is
fetched from a CDN at runtime. Two reasons that is worth the small hassle of
downloading a file:

- **The page keeps working.** A CDN outage, a DNS block, or a corporate proxy
  cannot take a self-hosted file away, and the version cannot change under you
  because someone re-tagged a release.
- **The Content-Security-Policy stays tight.** `app/core/middleware.py` sets
  `script-src 'self'`, so a `<script src="https://cdn...">` is refused by the
  browser. That is the policy working, not a bug -- vendor the file instead of
  loosening it.

Add one with the helper, which downloads the file and prints the tag to paste:

```bash
./scripts/vendor.sh https://cdnjs.cloudflare.com/ajax/libs/htmx.org/2.0.4/htmx.min.js
```

Then reference it from your HTML:

```html
<script src="/vendor/htmx.min.js"></script>
```

Commit what lands here. It is part of the app, not a build artifact.
