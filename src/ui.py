#!/usr/bin/env python3
"""Terminal presentation helpers. ANSI only, no dependencies."""

from __future__ import annotations

import os
import shutil
import sys
import time

NO_COLOR = bool(os.environ.get("NO_COLOR"))
SPEED = float(os.environ.get("NORTHWIND_SPEED", "1.0"))  # 0 = instant

WIDTH = min(shutil.get_terminal_size((100, 30)).columns, 100)


def _c(code: str) -> str:
    return "" if NO_COLOR else code


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
ITALIC = _c("\033[3m")

FG = {
    "cyan": _c("\033[38;5;51m"),
    "teal": _c("\033[38;5;44m"),
    "violet": _c("\033[38;5;141m"),
    "magenta": _c("\033[38;5;207m"),
    "amber": _c("\033[38;5;214m"),
    "gold": _c("\033[38;5;220m"),
    "green": _c("\033[38;5;84m"),
    "lime": _c("\033[38;5;154m"),
    "red": _c("\033[38;5;203m"),
    "grey": _c("\033[38;5;245m"),
    "dark": _c("\033[38;5;240m"),
    "white": _c("\033[38;5;255m"),
    "blue": _c("\033[38;5;75m"),
}

BG = {
    "red": _c("\033[48;5;52m"),
    "green": _c("\033[48;5;22m"),
    "violet": _c("\033[48;5;54m"),
    "amber": _c("\033[48;5;94m"),
    "blue": _c("\033[48;5;24m"),
}


def color(text: str, name: str, bold: bool = False) -> str:
    return f"{BOLD if bold else ''}{FG.get(name, '')}{text}{RESET}"


def pause(seconds: float) -> None:
    if SPEED > 0:
        time.sleep(seconds * SPEED)


def write(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def typed(text: str, delay: float = 0.012, prefix: str = "") -> None:
    """Type a line out character by character. Used sparingly, for the beats
    that are supposed to feel like someone is talking."""
    sys.stdout.write(prefix)
    if SPEED <= 0:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay * SPEED)
    sys.stdout.write("\n")
    sys.stdout.flush()


def rule(char: str = "─", tone: str = "dark") -> None:
    write(color(char * WIDTH, tone))


def banner(title: str, subtitle: str = "", tone: str = "violet") -> None:
    write()
    write(color("╭" + "─" * (WIDTH - 2) + "╮", tone))
    pad = WIDTH - 4 - len(title)
    write(color("│ ", tone) + BOLD + FG.get(tone, "") + title + RESET
          + " " * max(pad, 0) + color(" │", tone))
    if subtitle:
        pad = WIDTH - 4 - len(subtitle)
        write(color("│ ", tone) + FG["grey"] + subtitle + RESET
              + " " * max(pad, 0) + color(" │", tone))
    write(color("╰" + "─" * (WIDTH - 2) + "╯", tone))


def act(number: str, title: str, tone: str = "cyan") -> None:
    write()
    label = f" ACT {number} "
    write(color("━" * 3, tone) + BG.get("blue", "") + BOLD + FG["white"]
          + label + RESET + " " + BOLD + FG.get(tone, "") + title + RESET
          + " " + color("━" * max(WIDTH - len(label) - len(title) - 6, 0), "dark"))
    write()


def step(text: str, tone: str = "grey", icon: str = "·") -> None:
    write(f"  {color(icon, tone)} {color(text, tone)}")


def ok(text: str) -> None:
    write(f"  {color('✔', 'green', True)} {text}")


def bad(text: str) -> None:
    write(f"  {color('✘', 'red', True)} {text}")


def warn(text: str) -> None:
    write(f"  {color('▲', 'amber', True)} {text}")


def info(text: str) -> None:
    write(f"  {color('›', 'blue')} {text}")


def kv(key: str, value: str, width: int = 26, tone: str = "white") -> None:
    write(f"    {color(key.ljust(width), 'grey')} {color(value, tone)}")


def tool_call(name: str, args: str = "", identity: str = "") -> None:
    who = f" {color('as ' + identity, 'dark')}" if identity else ""
    write(f"  {color('⟶', 'violet')} {color('mcp', 'dark')} "
          f"{color(name, 'violet', True)}{color('(' + args + ')', 'dark')}{who}")


def tool_result(text: str, tone: str = "teal") -> None:
    write(f"    {color('←', 'dark')} {color(text, tone)}")


def spinner(text: str, seconds: float = 1.0, done: str = "") -> None:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    if SPEED <= 0:
        write(f"  {color('✔', 'green')} {done or text}")
        return
    end = time.time() + seconds * SPEED
    i = 0
    while time.time() < end:
        sys.stdout.write(f"\r  {color(frames[i % len(frames)], 'cyan')} {text}   ")
        sys.stdout.flush()
        time.sleep(0.07)
        i += 1
    sys.stdout.write("\r" + " " * (WIDTH - 1) + "\r")
    write(f"  {color('✔', 'green')} {done or text}")


def table(headers: list[str], rows: list[list[str]], tones: list[str] | None = None) -> None:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(_strip(cell)))
    header = "  " + "  ".join(
        color(h.upper().ljust(widths[i]), "dark") for i, h in enumerate(headers))
    write(header)
    write("  " + color("─" * (sum(widths) + 2 * (len(widths) - 1)), "dark"))
    for j, r in enumerate(rows):
        tone = (tones[j] if tones and j < len(tones) else "white")
        cells = []
        for i, cell in enumerate(r):
            padding = " " * (widths[i] - len(_strip(cell)))
            cells.append(cell + padding if "\033" in cell
                         else color(cell.ljust(widths[i]), tone))
        write("  " + "  ".join(cells))


def _strip(text: str) -> str:
    out, skip = [], False
    for ch in text:
        if ch == "\033":
            skip = True
        elif skip and ch == "m":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out)


def box(lines: list[str], tone: str = "dark", title: str = "") -> None:
    inner = WIDTH - 4
    top = "╭─" + (f" {title} ".ljust(inner, "─") if title else "─" * inner) + "─╮"
    write(color(top, tone))
    for line in lines:
        visible = _strip(line)
        pad = " " * max(inner - len(visible), 0)
        write(color("│ ", tone) + line[:len(line)] + pad + color(" │", tone))
    write(color("╰" + "─" * (inner + 2) + "╯", tone))


def wrap(text: str, width: int) -> list[str]:
    """Greedy wrap on plain text. Everything printed here is short enough that
    a real wrapper would be overkill, but a terminal recording is unforgiving
    about overflow."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def wrapped(text: str, tone: str, indent: int, first_prefix: str = "") -> None:
    pad = " " * indent
    avail = max(WIDTH - indent - len(_strip(first_prefix)) - 1, 24)
    lines = wrap(text, avail)
    write(f"{pad}{first_prefix}{color(lines[0], tone)}")
    for extra in lines[1:]:
        write(f"{pad}{' ' * len(_strip(first_prefix))}{color(extra, tone)}")


def finding(index: int, policy_id: str, message: str, remediation: str = "") -> None:
    head, _, _ = message.partition("->")
    prefix = f"{color(f'{index:>2}.', 'dark')} {color(policy_id, 'red', True)}  "
    wrapped(head.strip(), "white", 2, prefix)
    if remediation:
        wrapped(remediation, "amber", 6, color("fix → ", "dark"))
