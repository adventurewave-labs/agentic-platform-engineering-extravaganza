#!/usr/bin/env python3
"""
Turn a real ANSI capture into the HTML the showcase page displays.

This module exists because of a specific kind of lie the page used to tell. Two
blocks on it -- the Act V transcript and a rendered SQLInstance -- were hand-
written HTML, marked up to look like terminal output and badged "REAL OUTPUT"
and "REAL RENDER". The *values* in them were copied from real runs, so they
were accurate on the day they were pasted. They were still hand-written, and
they had already drifted: continuation lines had lost their indentation, and
the SQLInstance was showing seven parameters out of thirteen with its metadata
removed and a secret name replaced by an ellipsis.

The fix is not to re-copy them more carefully. It is to stop copying. The page
now renders these blocks from the program's own output at build time, through
this converter, so "REAL OUTPUT" is a description of the build step rather than
a claim about someone's diligence.

`ui.py` emits nothing but SGR colour codes -- no cursor movement, no carriage
returns -- which is what makes an exact conversion possible in eighty lines
instead of requiring a terminal emulator.
"""

from __future__ import annotations

import html
import re

# The page's palette, keyed by the xterm-256 code ui.py actually emits. Kept in
# this shape deliberately: if ui.py adds a colour, the assertion in `convert`
# fires rather than the page silently rendering it as default-coloured text.
FG = {
    "38;5;51": "#22d3ee",   # cyan
    "38;5;44": "#2dd4bf",   # teal
    "38;5;141": "#a78bfa",  # violet
    "38;5;207": "#f0abfc",  # magenta
    "38;5;214": "#fbbf24",  # amber
    "38;5;220": "#fcd34d",  # gold
    "38;5;84": "#4ade80",   # green
    "38;5;154": "#a3e635",  # lime
    "38;5;203": "#f87171",  # red
    "38;5;245": "#8b949e",  # grey
    "38;5;240": "#6e7681",  # dark
    "38;5;255": "#e6edf3",  # white
    "38;5;75": "#60a5fa",   # blue
}

BG = {
    "48;5;52": "#450a0a",
    "48;5;22": "#052e16",
    "48;5;54": "#2e1065",
    "48;5;94": "#451a03",
    "48;5;24": "#082f49",
}

SGR = re.compile(r"\033\[([0-9;]*)m")


class UnknownAnsiCode(ValueError):
    """A colour the page has no mapping for. Fail loudly rather than drop it."""


def convert(text: str) -> str:
    """ANSI -> HTML, preserving every space. Raises on an unmapped colour."""
    out: list[str] = []
    fg = bg = ""
    bold = False
    pos = 0
    depth = 0

    def open_span() -> str:
        style = []
        if fg:
            style.append(f"color:{fg}")
        if bg:
            style.append(f"background:{bg}")
        if bold:
            style.append("font-weight:600")
        return f'<span style="{";".join(style)}">' if style else ""

    for m in SGR.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        pos = m.end()
        code = m.group(1)

        if depth:
            out.append("</span>")
            depth = 0

        if code in ("", "0"):
            fg = bg = ""
            bold = False
        elif code == "1":
            bold = True
        elif code in FG:
            fg = FG[code]
        elif code in BG:
            bg = BG[code]
        elif code in ("2", "3"):
            pass  # dim / italic: the page's own styling covers these
        else:
            raise UnknownAnsiCode(
                f"ui.py emitted SGR {code!r}, which the page has no colour for. "
                f"Add it to ansi2html.FG/BG rather than letting the block render "
                f"as untinted text.")

        span = open_span()
        if span:
            out.append(span)
            depth = 1

    out.append(html.escape(text[pos:]))
    if depth:
        out.append("</span>")
    return "".join(out)


def strip(text: str) -> str:
    """The same capture with the colour removed -- for width and diff checks."""
    return SGR.sub("", text)
