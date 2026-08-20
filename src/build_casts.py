#!/usr/bin/env python3
"""
Build asciinema casts (and GIFs, if `agg` is present) from real demo runs.

The demo is executed for real and its stdout is captured verbatim; what you see
in a GIF is byte-for-byte what the tools printed.

Two things *are* synthesised, and it is worth saying so plainly: the per-line
timings (the capture runs at --speed 0, so a cast would otherwise dump 2000
lines into one frame), and the `./run.sh demo` prompt at the top (the capture
actually invokes `python3 src/goldenpath.py` directly). Neither changes a
single character of output. The highlight reel is a *selection* of acts from
one capture -- lines are dropped, never rewritten -- so it cannot claim
anything the full run did not.

    ./run.sh record            # all casts + GIFs
    ./run.sh record wow        # just the headline cast
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "recordings"
GIFS = ROOT / "gifs"
CAPTURED = ROOT / "captured"

COLS, ROWS = 100, 32

# The headline cut. Same bytes as the full run -- these are literally slices of
# one capture, not a re-enactment -- but only the acts that carry the argument,
# so the hero GIF stays watchable and stays under GitHub's inline budget.
HIGHLIGHT_ACTS = ["II", "III", "V", "VII", "SCORECARD"]

CASTS = {
    "highlight": {
        "title": "Agentic Platform Engineering — the argument in 90 seconds",
        "argv": ["--speed", "0"],
        "cols": 100, "rows": 32,
        "acts": HIGHLIGHT_ACTS,
    },
    "wow": {
        "title": "Agentic Platform Engineering — the whole story",
        "argv": ["--speed", "0"],
        "cols": 100, "rows": 32,
    },
    "policy_gate": {
        "title": "The policy gate: an agent reading real OPA denials and fixing the cause",
        "argv": ["--act", "5", "--speed", "0"],
        "cols": 100, "rows": 30,
    },
    "authz": {
        "title": "Authorization: the tool an agent is never told about",
        "argv": ["--act", "3", "--speed", "0"],
        "cols": 100, "rows": 30,
    },
    "render": {
        "title": "score.yaml to a Crossplane composite, via the platform's own provisioner",
        "argv": ["--act", "4", "--speed", "0"],
        "cols": 100, "rows": 30,
    },
    "drift": {
        "title": "Day 2: drift detection with attribution, and a diff instead of an apply",
        "argv": ["--act", "8", "--speed", "0"],
        "cols": 100, "rows": 30,
    },
}


def capture(argv: list[str], cols: int) -> str:
    env = dict(os.environ)
    env["COLUMNS"] = str(cols)
    env["NORTHWIND_SPEED"] = "0"
    env.pop("NO_COLOR", None)
    env["PATH"] = f"{ROOT / 'bin'}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "goldenpath.py"), *argv],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=600,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-3000:])
        raise SystemExit(f"demo exited {proc.returncode}")
    return proc.stdout


def delay_for(line: str) -> float:
    """How long to linger on a line. Structure gets a beat; body flows."""
    plain = strip_ansi(line).strip()
    if not plain:
        return 0.10
    if plain.startswith("━━━"):          # act header
        return 0.85
    if plain.startswith(("╭", "╰", "═══")):
        return 0.18
    if plain.startswith(("PASSED", "BLOCKED", "DENIED")) or "PASSED" in plain[:20]:
        return 1.10
    if plain.startswith("▍"):            # iteration marker
        return 0.55
    if plain[:3].strip().rstrip(".").isdigit():   # a numbered finding
        return 0.22
    if plain.startswith(("⟶", "←", "✔", "✘", "▲", "›")):
        return 0.30
    if plain.startswith("fix →"):
        return 0.22
    return 0.085 + min(len(plain), 90) * 0.0016


def strip_ansi(text: str) -> str:
    out, skip = [], False
    for ch in text:
        if ch == "\033":
            skip = True
        elif skip and ch == "m":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out)


ACT_MARK = "\u2501\u2501\u2501"  # the act rule


def select_acts(text: str, acts: list[str]) -> str:
    """Keep the banner plus the named acts, dropping everything else.

    Pure selection over one real capture. No line is rewritten, so the
    highlight reel cannot say anything the full run did not.
    """
    lines = text.splitlines()
    out: list[str] = []
    keeping = True          # the opening banner
    seen_any_act = False
    for line in lines:
        plain = strip_ansi(line)
        label = None
        if plain.startswith(ACT_MARK):
            if " ACT " in plain:
                label = plain.split(" ACT ", 1)[1].split(" ", 1)[0].strip()
            elif " SCORECARD " in plain:
                # The closing section is not numbered; it is still selectable.
                label = "SCORECARD"
        if label is not None:
            seen_any_act = True
            keeping = label in acts
            if keeping:
                out.append("")
        if keeping:
            out.append(line)
        elif not seen_any_act:
            out.append(line)
    return "\n".join(out)


def build_cast(name: str, spec: dict) -> Path:
    cols = spec.get("cols", COLS)
    rows = spec.get("rows", ROWS)
    text = capture(spec["argv"], cols)
    if spec.get("acts"):
        text = select_acts(text, spec["acts"])
    (CAPTURED / f"{name}.txt").write_text(strip_ansi(text))

    header = {
        "version": 2, "width": cols, "height": rows,
        "timestamp": 1755700000,
        "title": spec["title"],
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
    }
    events: list[list] = []
    t = 0.4

    prompt = (f"\033[38;5;84m➜\033[0m  \033[38;5;44mnorthwind\033[0m "
              f"\033[38;5;245m$\033[0m ")
    command = "./run.sh " + (
        "demo" if spec["argv"][:1] != ["--act"] else f"act {spec['argv'][1]}")
    events.append([t, "o", prompt])
    t += 0.35
    for ch in command:
        events.append([t, "o", ch])
        t += 0.028
    t += 0.45
    events.append([t, "o", "\r\n"])
    t += 0.35

    for line in text.splitlines():
        events.append([t, "o", line + "\r\n"])
        t += delay_for(line)

    t += 2.0
    events.append([t, "o", "\r\n" + prompt])

    RECORDINGS.mkdir(exist_ok=True)
    path = RECORDINGS / f"{name}.cast"
    with path.open("w") as fh:
        fh.write(json.dumps(header) + "\n")
        for e in events:
            fh.write(json.dumps([round(e[0], 3), e[1], e[2]]) + "\n")
    return path


GIF_TUNING = {
    # The full run is long. Render it small and fast; the highlight reel is the
    # one meant to be watched.
    "wow": {"font_size": "11", "speed": "2.6", "line_height": "1.2"},
    "highlight": {"font_size": "14", "speed": "1.7", "line_height": "1.3"},
}


def render_gif(name: str) -> bool:
    agg = ROOT / "bin" / "agg"
    if not agg.exists():
        agg_path = shutil.which("agg")
        if not agg_path:
            return False
        agg = Path(agg_path)
    GIFS.mkdir(exist_ok=True)
    proc = subprocess.run([
        str(agg),
        str(RECORDINGS / f"{name}.cast"),
        str(GIFS / f"{name}.gif"),
        "--font-family", "JetBrains Mono,DejaVu Sans Mono,Liberation Mono,monospace",
        "--theme", "1a1b26,c0caf5,15161e,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,a9b1d6,"
                   "414868,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,c0caf5",
        "--font-size", GIF_TUNING.get(name, {}).get("font_size", "15"),
        "--line-height", GIF_TUNING.get(name, {}).get("line_height", "1.35"),
        "--speed", GIF_TUNING.get(name, {}).get("speed", "1.0"),
        "--idle-time-limit", "1.5",
        "--last-frame-duration", "5",
    ], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:] + "\n")
        return False
    optimise(GIFS / f"{name}.gif")
    return True


def optimise(path: Path) -> None:
    """Squeeze the GIF if gifsicle is around. Optional -- the file is valid
    either way, just larger."""
    gifsicle = shutil.which("gifsicle")
    if not gifsicle:
        return
    tmp = path.with_suffix(".opt.gif")
    proc = subprocess.run(
        [gifsicle, "-O3", "--lossy=70", "--colors", "128",
         str(path), "-o", str(tmp)],
        capture_output=True, text=True)
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size < path.stat().st_size:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)


def main() -> int:
    RECORDINGS.mkdir(exist_ok=True)
    CAPTURED.mkdir(exist_ok=True)
    GIFS.mkdir(exist_ok=True)
    wanted = sys.argv[1:] or list(CASTS)
    for name in wanted:
        spec = CASTS.get(name)
        if not spec:
            print(f"unknown cast: {name}; known: {', '.join(CASTS)}")
            continue
        print(f"  building {name}.cast ...", flush=True)
        path = build_cast(name, spec)
        size = path.stat().st_size / 1024
        print(f"    {path.relative_to(ROOT)}  ({size:.0f} KiB)")
        print(f"  rendering {name}.gif ...", flush=True)
        if render_gif(name):
            gif = GIFS / f"{name}.gif"
            print(f"    {gif.relative_to(ROOT)}  ({gif.stat().st_size / 1024:.0f} KiB)")
        else:
            print("    agg not available — cast written, GIF skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
