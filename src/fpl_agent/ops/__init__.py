"""Scheduled jobs: short processes launchd runs on a timer.

Each job reads the repo ``.env`` into the environment, talks to the FPL public
API and to the local LMS over HTTP, writes a success marker that ``deadman``
watches, and may send one Telegram alert. None of them touches the FPL
account, and none of them reads anything under ``~/Desktop`` — that is why they
are Python modules here and not a shell script in the LMS repo: macOS refused
``/bin/bash`` there for seven weeks and nothing said so.
"""
