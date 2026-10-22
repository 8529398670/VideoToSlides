"""Google Slides export: OAuth for one user's Google account (oauth.py) and
the Drive upload that turns a .pptx into a Slides deck (drive.py).

Plain HTTPS calls through the standard library rather than Google's client
libraries: three endpoints do not justify a dependency tree, and keeping this
small keeps it auditable. Everything here is synchronous; routes call it
through run().
"""
