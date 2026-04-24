"""Picker UI subsystem (Sprint 3 / Cluster 4 — TRD-018..021).

Three modules cooperate to put a "pick which issues to fix" prompt in
front of the user without forcing a particular environment:

* :mod:`ghia.ui.server` builds a Starlette ASGI app that serves the
  static picker HTML and exposes the ``/api/issues`` and
  ``/api/confirm`` endpoints the page calls back into.
* :mod:`ghia.ui.terminal` renders a `rich`-table fallback for headless
  / SSH sessions and returns the same ``{queue, mode}`` contract as the
  browser's ``POST /api/confirm`` payload.
* :mod:`ghia.ui.opener` decides which path to take and orchestrates
  starting the local ASGI server + opening the browser when a display
  is available, otherwise dropping straight to the terminal picker.
"""
