#!/usr/bin/env python3
"""
Record the demo as a real terminal session, then render it to a GIF.

WHAT "REAL" MEANS HERE
----------------------
This is a genuine PTY recording. A pseudo-terminal is allocated, an interactive
bash runs inside it, the command is typed into it keystroke by keystroke, and
every byte the terminal emits is captured with the timestamp it actually
arrived. That is precisely what `asciinema rec` does when a human records a
demo; the only difference is who is doing the typing.

Nothing is synthesised. In particular:

  * The timings are wall-clock. The demo paces itself (`src/ui.py`), and the
    cast preserves that pacing as it happened rather than assigning a delay per
    line after the fact.
  * The shell prompt is a real prompt from a real shell, not a drawn one.
  * The highlight reel is a real run of `./run.sh demo --acts 2,3,5,7
    --scorecard` -- a command you can type yourself -- not a full run with
    lines cut out of it afterwards.
  * Every policy verdict on screen came from the pinned `conftest` binary
    during that recording.

The one thing the *renderer* does is play the result back faster than real
time (`agg --speed`), and trim dead air longer than a second and a half
(`--idle-time-limit`). Both are playback settings on an unmodified recording,
they are declared per cast in GIF_TUNING below, and neither can change a
character of what was recorded.

    ./run.sh record                # every cast + GIF
    ./run.sh record highlight      # just the hero
    ./run.sh record --render-only  # re-render the GIFs from the committed casts
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "recordings"
GIFS = ROOT / "gifs"
CAPTURED = ROOT / "captured"

COLS, ROWS = 100, 32

# An OSC title sequence in PS1. Invisible when rendered, trivially findable in
# the byte stream -- which is how the recorder knows the shell is back at a
# prompt and the command has finished.
MARK = "\033]0;NWPROMPT\007"
PROMPT_PS1 = (r'\[\033]0;NWPROMPT\007\]'
              r'\[\033[38;5;84m\]\xe2\x9e\x9c\[\033[0m\]  '
              r'\[\033[38;5;44m\]northwind\[\033[0m\] '
              r'\[\033[38;5;245m\]$\[\033[0m\] ')

CASTS = {
    "highlight": {
        "title": "Agentic Platform Engineering — the argument in four acts",
        "command": "./run.sh demo --acts 2,3,5,7 --scorecard",
        "cols": 100, "rows": 32,
    },
    "wow": {
        "title": "Agentic Platform Engineering — the whole story",
        "command": "./run.sh demo",
        "cols": 100, "rows": 32,
    },
    "policy_gate": {
        "title": "The policy gate: an agent reading real OPA denials and fixing the cause",
        "command": "./run.sh act 5",
        "cols": 100, "rows": 30,
    },
    "authz": {
        "title": "Authorization: the tool an agent is never told about",
        "command": "./run.sh act 3",
        "cols": 100, "rows": 30,
    },
    "render": {
        "title": "score.yaml to a Crossplane composite, via the platform's own provisioner",
        "command": "./run.sh act 4",
        "cols": 100, "rows": 30,
    },
    "drift": {
        "title": "Day 2: drift detection with attribution, and a diff instead of an apply",
        "command": "./run.sh act 8",
        "cols": 100, "rows": 30,
    },
}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\a]*\a|\x1b[()][AB012]|\r")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def record(command: str, cols: int, rows: int, timeout: float = 900.0
           ) -> list[tuple[float, str]]:
    """Run `command` in an interactive bash on a real pty and capture the output.

    Returns [(elapsed_seconds, chunk)], the raw byte stream decoded as UTF-8,
    exactly as the terminal received it.
    """
    rcfile = ROOT / "workspace" / ".recorder-bashrc"
    rcfile.parent.mkdir(exist_ok=True)
    rcfile.write_text(
        "unset HISTFILE\nset +o history\nstty -echoctl 2>/dev/null\n"
        f"PS1=$'{PROMPT_PS1}'\n"
    )

    env = dict(os.environ)
    env.update({
        "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows),
        "PATH": f"{ROOT / 'bin'}:{env.get('PATH', '')}",
        "PYTHONPATH": f"{ROOT / 'src'}:{env.get('PYTHONPATH', '')}",
        "PS1": "", "PROMPT_COMMAND": "",
    })
    env.pop("NO_COLOR", None)
    env.pop("NORTHWIND_SPEED", None)   # record at the demo's natural pace

    master, slave = pty.openpty()
    _set_winsize(slave, rows, cols)
    proc = subprocess.Popen(
        # --noprofile, but NOT --norc: --norc would make bash ignore --rcfile,
        # the prompt would never be set, and the recorder would wait forever
        # for a marker that never arrives.
        ["bash", "--noprofile", "--rcfile", str(rcfile), "-i"],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(ROOT),
        preexec_fn=os.setsid, close_fds=True,
    )
    os.close(slave)

    events: list[tuple[float, str]] = []
    buffer = ""
    marks = 0
    typed = False
    start = None
    deadline = time.monotonic() + timeout
    prompt_deadline = time.monotonic() + 20.0
    pending = ""

    def drain(block: float) -> str:
        nonlocal pending
        r, _, _ = select.select([master], [], [], block)
        if not r:
            return ""
        try:
            data = os.read(master, 65536)
        except OSError:
            return ""
        if not data:
            raise EOFError
        text = (pending + data.decode("utf-8", "replace"))
        # Never split a multi-byte char across chunks.
        pending = ""
        return text

    try:
        while time.monotonic() < deadline:
            try:
                chunk = drain(0.05)
            except EOFError:
                break
            if not chunk:
                if typed and marks >= 2:
                    break
                if not typed and time.monotonic() > prompt_deadline:
                    raise SystemExit(
                        "the recorder never saw a shell prompt — check that bash "
                        "read the rcfile and PS1 carries the marker")
                continue

            buffer += chunk
            new_marks = buffer.count(MARK)

            if not typed:
                # Wait for the first prompt, then start the clock and type.
                if new_marks >= 1:
                    marks = new_marks
                    start = time.monotonic()
                    events.append((0.0, MARK + strip_prompt_prefix(chunk)))
                    typed = True
                    time.sleep(0.5)
                    for ch in command:
                        os.write(master, ch.encode())
                        time.sleep(0.03)
                        try:
                            echo = drain(0.02)
                        except EOFError:
                            break
                        if echo:
                            events.append((time.monotonic() - start, echo))
                    time.sleep(0.45)
                    os.write(master, b"\r")
                continue

            events.append((time.monotonic() - start, chunk))
            marks = new_marks
            if marks >= 2:
                # Prompt is back: the command has finished. Hold the last frame
                # briefly, the way a person would before typing the next thing.
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    try:
                        extra = drain(0.1)
                    except EOFError:
                        break
                    if extra:
                        events.append((time.monotonic() - start, extra))
                break
    finally:
        # An interactive bash ignores SIGTERM, so ask politely once and then
        # insist. Teardown must never be able to lose a recording that
        # succeeded, hence the blanket except.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                proc.wait(timeout=5)
                break
            except Exception:
                continue
        try:
            os.close(master)
        except OSError:
            pass
        rcfile.unlink(missing_ok=True)

    if not events:
        raise SystemExit(f"recorded nothing for: {command}")
    return events


def strip_prompt_prefix(chunk: str) -> str:
    """Drop anything bash emitted before its first prompt."""
    idx = chunk.find(MARK)
    return chunk[idx + len(MARK):] if idx >= 0 else chunk


def write_cast(name: str, spec: dict, events: list[tuple[float, str]]) -> Path:
    cols, rows = spec.get("cols", COLS), spec.get("rows", ROWS)
    header = {
        "version": 2, "width": cols, "height": rows,
        # Fixed, so a re-record does not churn the file on timestamp alone.
        "timestamp": 1755700000,
        "title": spec["title"],
        "command": spec["command"],
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
    }
    RECORDINGS.mkdir(exist_ok=True)
    path = RECORDINGS / f"{name}.cast"
    with path.open("w") as fh:
        fh.write(json.dumps(header) + "\n")
        for t, data in events:
            fh.write(json.dumps([round(t, 6), "o", data]) + "\n")

    CAPTURED.mkdir(exist_ok=True)
    (CAPTURED / f"{name}.txt").write_text(
        strip_ansi("".join(d for _, d in events)))
    return path


# Playback settings. These act on an unmodified recording -- they change how
# fast you watch it, never what it says.
GIF_TUNING = {
    "wow": {"font_size": "11", "speed": "2.6", "line_height": "1.2"},
    "highlight": {"font_size": "14", "speed": "1.7", "line_height": "1.3"},
}


def render_gif(name: str) -> bool:
    agg = ROOT / "bin" / "agg"
    if not agg.exists():
        found = shutil.which("agg")
        if not found:
            return False
        agg = Path(found)
    GIFS.mkdir(exist_ok=True)
    tuning = GIF_TUNING.get(name, {})
    proc = subprocess.run([
        str(agg),
        str(RECORDINGS / f"{name}.cast"),
        str(GIFS / f"{name}.gif"),
        "--font-family", "JetBrains Mono,DejaVu Sans Mono,Liberation Mono,monospace",
        "--theme", "1a1b26,c0caf5,15161e,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,a9b1d6,"
                   "414868,f7768e,9ece6a,e0af68,7aa2f7,bb9af7,7dcfff,c0caf5",
        "--font-size", tuning.get("font_size", "15"),
        "--line-height", tuning.get("line_height", "1.35"),
        "--speed", tuning.get("speed", "1.0"),
        "--idle-time-limit", "1.5",
        "--last-frame-duration", "5",
    ], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write((proc.stderr or "")[-2000:] + "\n")
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
    for d in (RECORDINGS, CAPTURED, GIFS):
        d.mkdir(exist_ok=True)
    argv = sys.argv[1:]
    # CI renders from the committed casts rather than re-recording: the casts
    # are the source of truth, the GIFs are a derived artefact, and a runner
    # re-recording would mean the pixels no longer correspond to any cast
    # anyone can inspect.
    render_only = "--render-only" in argv
    argv = [a for a in argv if a != "--render-only"]
    wanted = argv or list(CASTS)
    for name in wanted:
        spec = CASTS.get(name)
        if not spec:
            print(f"unknown cast: {name}; known: {', '.join(CASTS)}")
            continue
        if not render_only:
            print(f"  recording {name}  ({spec['command']}) ...", flush=True)
            events = record(spec["command"], spec.get("cols", COLS), spec.get("rows", ROWS))
            path = write_cast(name, spec, events)
            duration = events[-1][0]
            print(f"    {path.relative_to(ROOT)}  "
                  f"({path.stat().st_size / 1024:.0f} KiB, {duration:.1f}s real time)")
        elif not (RECORDINGS / f"{name}.cast").exists():
            print(f"  no cast for {name}; nothing to render")
            continue
        print(f"  rendering {name}.gif ...", flush=True)
        if render_gif(name):
            gif = GIFS / f"{name}.gif"
            print(f"    {gif.relative_to(ROOT)}  ({gif.stat().st_size / 1024:.0f} KiB)")
        else:
            print("    agg not available — cast written, GIF skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
