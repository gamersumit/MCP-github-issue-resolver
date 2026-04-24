"""Shipped static assets for the picker UI (``picker.html``).

This package only exists to make the surrounding directory a discoverable
subpackage of :mod:`ghia` so the bundled HTML asset travels with the
wheel. The asset itself is served via Starlette's ``FileResponse`` from
:func:`ghia.ui.server.picker_html_path`; nothing here is meant to be
imported.
"""
