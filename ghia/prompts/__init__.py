"""Shipped prompt templates (e.g. ``agent_protocol.md``).

This package only exists to make the surrounding directory a discoverable
subpackage of :mod:`ghia` so the bundled ``.md`` assets travel with the
wheel. The assets themselves are loaded via ordinary filesystem reads
through :func:`ghia.protocol.template_path`; nothing here is meant to be
imported.
"""
