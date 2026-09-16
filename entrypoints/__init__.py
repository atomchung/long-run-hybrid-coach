"""Packaging around the one coach, never a second implementation of it.

Most of what is under here is documentation: an entry is usually a client pointed at the
hosted gateway, and it ships as setup mechanics rather than as code. ``demo`` is the one
that needs a runtime of its own, so this directory is an importable package. Nothing in
``garmin_coach_loop`` imports anything here, and ``tests/test_demo_boundary.py`` fails if
that ever stops being true.
"""
