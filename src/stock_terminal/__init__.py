"""Project Watchtower (package stock_terminal): a read-only news, quant-read and validity terminal.

This package places no orders and contains no trading code. Every number it shows
travels inside a DataPoint that carries its source, its age and its status, so a
missing or stale value is shown as exactly that, never as a made-up number.
"""

__version__ = "0.1.0"
