"""fpl-agent — an FPL analyst that recommends, and never executes.

There is no authenticated code path in this package. Every endpoint used is
public, so the tool can be run by anyone against any entry ID without
credentials, and cannot modify a team even by accident.
"""

__version__ = "0.1.0"
