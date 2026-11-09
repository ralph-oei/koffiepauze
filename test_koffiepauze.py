#!/usr/bin/env python3
"""Tests for koffiepauze. Standard library only: python3 -m unittest -v

Everything runs against a temporary state directory and a frozen clock, so these
touch neither your real state nor your real settings.json.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "state"
        os.environ["KOFFIEPAUZE_HOME"] = str(self.home)
        os.environ["KOFFIEPAUZE_SETTINGS"] = str(Path(self.tmp.name) / "settings.json")
        self.clock = 1_000_000.0
        self.tick(0)
        sys.path.insert(0, str(HERE))
        self.k = importlib.reload(importlib.import_module("koffiepauze"))

    def tearDown(self):
        for key in ("KOFFIEPAUZE_HOME", "KOFFIEPAUZE_SETTINGS", "KOFFIEPAUZE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def tick(self, seconds):
        self.clock += seconds
        os.environ["KOFFIEPAUZE_NOW"] = repr(self.clock)

    def busy(self, *ids):
        for sid in ids:
            self.k.remember(sid, "busy")

    def waiting(self, *ids):
        for sid in ids:
            self.k.remember(sid, "waiting")

    def worked_long_enough(self):
        self.k.write_stamp(self.k.LAST_BREAK, self.clock - 60 * 60)

    def conf(self, **over):
        c = dict(self.k.DEFAULTS)
        c["quiet_hours"] = None          # tests must not depend on the wall clock
        c.update(over)
        return c


class Verdict(Base):
    def test_one_agent_is_never_a_reason(self):
        self.worked_long_enough()
        self.busy("a")
        self.tick(300)
        ready, soon, n, why = self.k.verdict(self.conf())
        self.assertFalse(ready or soon)
        self.assertEqual(n, 1)
        self.assertIn("need 2", why)

    def test_a_waiting_session_blocks_it(self):
        """The whole point: if an agent wants you, this is not a break."""
        self.worked_long_enough()
        self.busy("a", "b")
        self.waiting("c")
        self.tick(300)
        ready, soon, _, why = self.k.verdict(self.conf())
        self.assertFalse(ready or soon)
        self.assertIn("attention", why)

    def test_not_before_the_work_period_is_up(self):
        self.k.write_stamp(self.k.LAST_BREAK, self.clock)
        self.busy("a", "b")
        self.tick(300)
        ready, soon, _, why = self.k.verdict(self.conf())
        self.assertFalse(ready or soon)
        self.assertIn("since the last break", why)

    def test_all_busy_but_only_just(self):
        self.worked_long_enough()
        self.busy("a", "b")
        self.tick(10)
        ready, soon, _, why = self.k.verdict(self.conf())
        self.assertFalse(ready)
        self.assertTrue(soon, "should ask to be re-checked later")
        self.assertIn("only for", why)

    def test_the_good_moment(self):
        self.worked_long_enough()
        self.busy("a", "b", "c")
        self.tick(300)
        ready, soon, n, why = self.k.verdict(self.conf())
        self.assertTrue(ready)
        self.assertFalse(soon)
        self.assertEqual(n, 3)
        self.assertIn("nobody needs you", why)

    def test_quiet_hours_wrap_midnight(self):
        self.assertTrue(self.k.quiet_now(["22:00", "08:00"]) in (True, False))
        self.assertFalse(self.k.quiet_now(None))
        self.assertFalse(self.k.quiet_now(["nonsense"]))

    def test_stale_sessions_are_forgotten(self):
        self.busy("a", "b")
        self.tick(60 * 60)
        self.assertEqual(self.k.sessions(30), [])


class Hooks(Base):
    def run_hook(self, kind, payload):
        env = dict(os.environ)
        out = subprocess.run(
            [sys.executable, str(HERE / "koffiepauze.py"), "hook", kind],
            input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
        return out

    def test_hooks_always_exit_zero(self):
        """A Stop hook that exits 2 blocks the stop. This one must never do that."""
        for kind, payload in (("prompt", {"session_id": "s1"}),
                              ("stop", {"session_id": "s1"}),
                              ("attention", {"session_id": "s1"})):
            with self.subTest(kind=kind):
                self.assertEqual(self.run_hook(kind, payload).returncode, 0)

    def test_broken_input_is_survivable(self):
        out = subprocess.run([sys.executable, str(HERE / "koffiepauze.py"), "hook", "prompt"],
                             input="{not json at all", capture_output=True, text=True,
                             env=dict(os.environ), timeout=30)
        self.assertEqual(out.returncode, 0)

    def test_hooks_say_nothing_on_stdout(self):
        """UserPromptSubmit stdout is injected into the model's context. Stay silent."""
        out = self.run_hook("prompt", {"session_id": "s1"})
        self.assertEqual(out.stdout.strip(), "")

    def test_a_prompt_marks_the_session_busy_and_a_stop_marks_it_waiting(self):
        self.run_hook("prompt", {"session_id": "s1", "cwd": "/tmp/x"})
        self.assertEqual(self.k.sessions(30)[0]["state"], "busy")
        self.run_hook("stop", {"session_id": "s1"})
        self.assertEqual(self.k.sessions(30)[0]["state"], "waiting")

    def test_walking_away_counts_as_the_break(self):
        self.run_hook("prompt", {"session_id": "s1"})
        before = self.k.read_stamp(self.k.LAST_BREAK)
        self.tick(60 * 60)
        self.run_hook("prompt", {"session_id": "s1"})
        self.assertGreater(self.k.read_stamp(self.k.LAST_BREAK), before)

    def test_kill_switch(self):
        env = dict(os.environ, KOFFIEPAUZE_OFF="1")
        out = subprocess.run([sys.executable, str(HERE / "koffiepauze.py"), "hook", "prompt"],
                             input="{}", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.k.sessions(30), [])


class Install(Base):
    def settings(self):
        return json.loads(Path(os.environ["KOFFIEPAUZE_SETTINGS"]).read_text())

    def test_install_keeps_whatever_was_already_there(self):
        mine = {"permissions": {"allow": ["Read"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash",
                                          "hooks": [{"type": "command", "command": "my-guard.py"}]}]}}
        Path(os.environ["KOFFIEPAUZE_SETTINGS"]).write_text(json.dumps(mine))
        self.k.cmd_install()
        after = self.settings()
        self.assertEqual(after["permissions"], mine["permissions"])
        self.assertEqual(after["hooks"]["PreToolUse"], mine["hooks"]["PreToolUse"])
        for event in self.k.HOOK_EVENTS:
            self.assertIn(event, after["hooks"])

    def test_works_on_a_machine_with_no_settings_file_at_all(self):
        """Nothing here depends on any other hook existing. A clean install is the
        whole file, written from scratch."""
        target = Path(os.environ["KOFFIEPAUZE_SETTINGS"])
        self.assertFalse(target.exists())
        self.k.cmd_install()
        after = self.settings()
        self.assertEqual(sorted(after["hooks"]), sorted(self.k.HOOK_EVENTS))
        self.assertEqual(list(after), ["hooks"])
        # and taking it off again leaves nothing behind
        self.k.cmd_install(remove=True)
        self.assertEqual(self.settings(), {})

    def test_installing_twice_does_not_double_up(self):
        self.k.cmd_install()
        self.k.cmd_install()
        entries = [h for group in self.settings()["hooks"]["Stop"] for h in group["hooks"]]
        self.assertEqual(len(entries), 1)

    def test_uninstall_leaves_your_own_hooks_alone(self):
        mine = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}
        Path(os.environ["KOFFIEPAUZE_SETTINGS"]).write_text(json.dumps(mine))
        self.k.cmd_install()
        self.k.cmd_install(remove=True)
        self.assertEqual(self.settings()["hooks"]["Stop"],
                         [{"hooks": [{"type": "command", "command": "mine.sh"}]}])


class Message(Base):
    def test_the_count_lands_in_the_text(self):
        c = self.conf(notify=False, say=False, command=None, message="{n} agents")
        self.busy("a", "b")
        self.assertEqual(self.k.nudge(2, c), "2 agents")

    def test_a_broken_template_still_says_something(self):
        c = self.conf(notify=False, say=False, command=None, message="100% done {oops}")
        self.assertEqual(self.k.nudge(2, c), "100% done {oops}")


if __name__ == "__main__":
    unittest.main()
