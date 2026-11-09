#!/usr/bin/env python3
"""koffiepauze: a break nudge for people who run several coding agents at once.

With one agent you stand up while it works. With five, something is always finishing,
so there is no natural moment to stop. This watches the agents instead of the clock:
it only speaks up when every session it knows about is busy and none of them wants
anything from you.

Standard library only. Wire it into Claude Code hooks with:

    python3 koffiepauze.py install

Commands:
    install / uninstall     add or remove the hooks in ~/.claude/settings.json
    hook prompt|stop|attention   hook entry points (read a hook event on stdin)
    check                   the delayed re-check that actually nudges
    status                  print what it currently thinks is going on
    nudge                   deliver a test nudge right now

It never blocks your work: every path exits 0, and errors land in the log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("KOFFIEPAUZE_HOME") or Path.home() / ".koffiepauze")
SESSIONS = HOME / "sessions"
CONFIG = HOME / "config.json"
LAST_BREAK = HOME / "last-break"
LAST_EVENT = HOME / "last-event"
PENDING = HOME / "pending-check"
LOGFILE = HOME / "koffiepauze.log"

SETTINGS = Path(os.environ.get("KOFFIEPAUZE_SETTINGS") or Path.home() / ".claude" / "settings.json")
HOOK_EVENTS = {"UserPromptSubmit": "prompt", "Stop": "stop", "Notification": "attention"}

DEFAULTS = {
    # How many sessions must be running before this thing has an opinion at all.
    "min_agents": 2,
    # Don't nudge until you have been at it this long since the last break.
    "work_minutes": 50,
    # After the last session goes busy, wait this long and check again. Keeps the
    # nudge away from runs that finish in 5 seconds.
    "settle_seconds": 90,
    # A session nobody has touched for this long is assumed closed.
    "stale_minutes": 30,
    # No hook events for this long means you already walked away. Resets the clock.
    "idle_minutes": 20,
    # ["22:00", "08:00"] stays quiet overnight. null disables.
    "quiet_hours": ["22:00", "08:00"],
    "notify": True,
    "say": True,
    "voice": None,
    # Shell command for your own channel (ntfy, Slack, a smart speaker). The text is
    # in $KOFFIEPAUZE_MESSAGE and is also passed as the first argument.
    "command": None,
    "message": "Coffee break. All {n} agents are busy and none of them needs you.",
}


# ─── plumbing ───────────────────────────────────────────────────────────────
def now() -> float:
    override = os.environ.get("KOFFIEPAUZE_NOW")
    return float(override) if override else time.time()


def log(line: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%F %T"), line))
    except OSError:
        pass


def cfg() -> dict:
    c = dict(DEFAULTS)
    try:
        c.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    for key in DEFAULTS:
        env = os.environ.get("KOFFIEPAUZE_" + key.upper())
        if env is None:
            continue
        try:
            c[key] = json.loads(env)
        except ValueError:
            c[key] = env
    return c


def read_stamp(path: Path) -> float:
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def write_stamp(path: Path, value: float) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        path.write_text("%r\n" % value, encoding="utf-8")
    except OSError:
        pass


# ─── sessions ───────────────────────────────────────────────────────────────
def session_file(sid: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in sid)[:80]
    return SESSIONS / ("%s.json" % (safe or "unknown"))


def remember(sid: str, state: str, cwd: str = "") -> None:
    """state is 'busy' (working for you) or 'waiting' (wants something from you)."""
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        path = session_file(sid)
        was = {}
        try:
            was = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        since = was.get("since", 0) if was.get("state") == state else now()
        path.write_text(json.dumps({
            "state": state, "since": since, "updated": now(), "cwd": cwd or was.get("cwd", ""),
        }), encoding="utf-8")
    except OSError:
        pass


def sessions(stale_minutes: int) -> list:
    out = []
    cutoff = now() - stale_minutes * 60
    try:
        files = sorted(SESSIONS.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("updated", 0) < cutoff:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        rec["id"] = path.stem
        out.append(rec)
    return out


# ─── the decision ───────────────────────────────────────────────────────────
def quiet_now(window) -> bool:
    if not window:
        return False
    try:
        start, end = window
        sh, sm = (int(x) for x in str(start).split(":"))
        eh, em = (int(x) for x in str(end).split(":"))
    except (TypeError, ValueError):
        return False
    stamp = datetime.fromtimestamp(now())
    minute = stamp.hour * 60 + stamp.minute
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m > end_m:          # the window wraps past midnight
        return minute >= start_m or minute < end_m
    return start_m <= minute < end_m


def verdict(c: dict) -> tuple:
    """Returns (nudge_now, wait_a_bit, how_many, why). Pure, so it is testable."""
    live = sessions(c["stale_minutes"])
    if len(live) < c["min_agents"]:
        return False, False, len(live), "only %d session(s), need %d" % (len(live), c["min_agents"])
    waiting = [s for s in live if s.get("state") != "busy"]
    if waiting:
        return False, False, len(live), "%d session(s) want your attention" % len(waiting)
    since_break = now() - read_stamp(LAST_BREAK)
    if since_break < c["work_minutes"] * 60:
        return False, False, len(live), "only %d min since the last break" % int(since_break / 60)
    if quiet_now(c["quiet_hours"]):
        return False, False, len(live), "quiet hours"
    youngest = max(s.get("since", 0) for s in live)
    busy_for = now() - youngest
    if busy_for < c["settle_seconds"]:
        return False, True, len(live), "all busy, but only for %ds" % int(busy_for)
    return True, False, len(live), "all %d busy for %ds, nobody needs you" % (len(live), int(busy_for))


# ─── delivery ───────────────────────────────────────────────────────────────
def run(args, env=None) -> None:
    try:
        subprocess.run(args, env=env, timeout=20, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log("delivery failed: %s" % traceback.format_exc(limit=1).strip())


def deliver(message: str, c: dict) -> None:
    if c.get("command"):
        run(["sh", "-lc", '%s "$1"' % c["command"], "sh", message],
            env=dict(os.environ, KOFFIEPAUZE_MESSAGE=message))
    if sys.platform == "darwin":
        if c.get("notify"):
            run(["osascript", "-e", "display notification %s with title \"koffiepauze\""
                 % json.dumps(message)])
        if c.get("say"):
            run(["say"] + (["-v", str(c["voice"])] if c.get("voice") else []) + [message])
    elif sys.platform.startswith("linux"):
        if c.get("notify"):
            run(["notify-send", "koffiepauze", message])
        if c.get("say"):
            run(["spd-say", message])


def nudge(count: int, c: dict) -> str:
    template = str(c.get("message") or DEFAULTS["message"])
    try:
        message = template.format(n=count)
    except (KeyError, IndexError, ValueError):
        message = template
    write_stamp(LAST_BREAK, now())
    deliver(message, c)
    log("nudged (%d agents): %s" % (count, message))
    return message


# ─── commands ───────────────────────────────────────────────────────────────
def schedule_check(c: dict) -> None:
    """Hooks only fire on transitions, so the moment everything goes busy is the last
    event we see. Come back and look again once they have settled."""
    if now() - read_stamp(PENDING) < c["settle_seconds"] + 30:
        return
    write_stamp(PENDING, now())
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "check"],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        log("could not schedule: %s" % traceback.format_exc(limit=1).strip())


def cmd_hook(kind: str) -> int:
    c = cfg()
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}

    gap = now() - read_stamp(LAST_EVENT)
    if read_stamp(LAST_EVENT) and gap > c["idle_minutes"] * 60:
        # You were away. That counts as the break, so start the clock over.
        write_stamp(LAST_BREAK, now())
        log("idle for %d min, clock reset" % int(gap / 60))
    write_stamp(LAST_EVENT, now())
    if not read_stamp(LAST_BREAK):
        write_stamp(LAST_BREAK, now())

    sid = str(event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or os.getppid())
    remember(sid, "busy" if kind == "prompt" else "waiting", str(event.get("cwd") or ""))
    if kind == "prompt":
        ready, soon, _, _ = verdict(c)
        # Never nudge from inside the hook itself: speaking takes seconds and the hook
        # runs before your prompt does. The detached check has all the time it wants.
        if ready or soon:
            schedule_check(c)
    return 0


def cmd_check() -> int:
    c = cfg()
    time.sleep(min(c["settle_seconds"], 600))
    ready, _, count, why = verdict(c)
    if ready:
        nudge(count, c)
    else:
        log("check: no nudge (%s)" % why)
    try:
        PENDING.unlink()
    except OSError:
        pass
    return 0


def cmd_status() -> int:
    c = cfg()
    live = sessions(c["stale_minutes"])
    ready, soon, count, why = verdict(c)
    print("koffiepauze  state in %s" % HOME)
    print("  sessions seen: %d (need %d)" % (len(live), c["min_agents"]))
    for s in live:
        print("    %-12s %-7s for %4ds  %s"
              % (s["id"][:12], s.get("state", "?"), int(now() - s.get("since", now())),
                 s.get("cwd", "")))
    since = read_stamp(LAST_BREAK)
    print("  last break: %s" % (datetime.fromtimestamp(since).strftime("%H:%M") if since else "never"))
    print("  verdict: %s" % why)
    print("  would nudge now: %s%s" % (ready, " (re-check pending)" if soon else ""))
    return 0


def _hook_entry(path: str, event: str) -> dict:
    # Deliberately "python3" off the PATH and not sys.executable: on a machine with
    # Homebrew python, sys.executable is a versioned path that breaks on the next
    # minor upgrade, and a hook that silently stops running is worse than no hook.
    runner = os.environ.get("KOFFIEPAUZE_PYTHON", "python3")
    return {"type": "command",
            "command": "%s %s hook %s" % (runner, path, HOOK_EVENTS[event]),
            "timeout": 10}


def cmd_install(remove: bool = False) -> int:
    path = os.path.abspath(__file__)
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(".json.koffiepauze-backup")
        backup.write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        print("backed up your settings to %s" % backup)

    hooks = settings.setdefault("hooks", {})
    changed = []
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        # Drop any koffiepauze entry that is already there, then add if installing.
        for group in list(groups):
            group["hooks"] = [h for h in group.get("hooks", [])
                              if "koffiepauze" not in str(h.get("command", ""))]
            if not group["hooks"]:
                groups.remove(group)
        if not remove:
            groups.append({"hooks": [_hook_entry(path, event)]})
            changed.append(event)
        if not groups:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    if remove:
        print("removed the koffiepauze hooks from %s" % SETTINGS)
    else:
        print("installed hooks for %s in %s" % (", ".join(changed), SETTINGS))
        print("open a new Claude Code session (or run /hooks once) to load them")
    return 0


def main() -> int:
    args = sys.argv[1:]
    command = args[0] if args else "status"
    if os.environ.get("KOFFIEPAUZE_OFF") and command in ("hook", "check"):
        return 0
    try:
        if command == "hook":
            return cmd_hook(args[1] if len(args) > 1 else "prompt")
        if command == "check":
            return cmd_check()
        if command == "status":
            return cmd_status()
        if command == "install":
            return cmd_install()
        if command == "uninstall":
            return cmd_install(remove=True)
        if command == "nudge":
            c = cfg()
            print(nudge(len(sessions(c["stale_minutes"])) or 2, c))
            return 0
        print(__doc__)
        return 0
    except Exception:
        log("crash in '%s': %s" % (command, traceback.format_exc()))
        # A hook that fails must never hold up the session it is attached to.
        return 0


if __name__ == "__main__":
    sys.exit(main())
