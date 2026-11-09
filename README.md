# koffiepauze

A break nudge for people who run several coding agents at once.

With one agent you stand up while it works. With five, something is always finishing, so there is no natural moment to stop. Your coffee goes cold under the machine while you check "just one more" window.

Break reminders already exist. They watch the clock, which means they interrupt you in the middle of a test result as happily as they catch you idle. **koffiepauze watches the agents instead.** It only speaks up at the one moment that is actually free: every session it knows about is busy, and none of them wants anything from you.

Works with [Claude Code](https://claude.com/claude-code) hooks. Standard library Python, no dependencies, nothing leaves your machine.

## Install

```bash
git clone https://github.com/ralph-oei/koffiepauze.git
cd koffiepauze
python3 koffiepauze.py install
```

That adds three hooks to `~/.claude/settings.json` and backs up the old file next to it. Open a new Claude Code session, or run `/hooks` once, to load them. Everything else in your settings, including hooks you already had, is left alone.

```bash
python3 koffiepauze.py uninstall   # removes only its own entries
```

## How it decides

Three hooks tell it what each session is doing:

| hook | meaning |
|---|---|
| `UserPromptSubmit` | that session is now **busy** working for you |
| `Stop` | it finished and is **waiting** for you |
| `Notification` | it needs a permission or an answer, so it is **waiting** too |

It nudges when all five of these hold:

1. at least `min_agents` sessions are known (default 2)
2. none of them is waiting for you
3. they have all been busy for at least `settle_seconds` (default 90), so this is not a run that ends in five seconds
4. it has been at least `work_minutes` since your last break (default 50)
5. you are not inside `quiet_hours`

If you walk away for `idle_minutes` with no hook events at all, that counts as the break you were going to take, and the clock starts over.

Hooks only fire on transitions, so the moment everything goes busy is the last event it sees. When that happens it spawns one detached check that waits out the settle period and looks again. If an agent came back for you in the meantime, nothing happens.

```bash
python3 koffiepauze.py status   # what it thinks is going on right now
python3 koffiepauze.py nudge    # fire one, to test your channel
```

## Configuration

Optional `~/.koffiepauze/config.json`. See `config.example.json`. Every key can also be set per run as an environment variable: `KOFFIEPAUZE_WORK_MINUTES=25`.

| key | default | |
|---|---|---|
| `min_agents` | `2` | fewer sessions than this and it stays quiet |
| `work_minutes` | `50` | minimum stretch of work before a nudge |
| `settle_seconds` | `90` | how long everything must stay busy |
| `stale_minutes` | `30` | a session nobody touched for this long is assumed closed |
| `idle_minutes` | `20` | a gap this long counts as a break you already took |
| `quiet_hours` | `["22:00","08:00"]` | `null` to disable |
| `notify` | `true` | desktop notification |
| `say` | `true` | speak it out loud (`say` on macOS, `spd-say` on Linux) |
| `voice` | `null` | e.g. `"Xander"` on macOS |
| `command` | `null` | your own channel, see below |
| `message` | see below | `{n}` becomes the number of busy agents |

Default message: `Coffee break. All {n} agents are busy and none of them needs you.`

`command` runs through `sh -lc` with the text as `$KOFFIEPAUZE_MESSAGE` and as `$1`, so you can send it anywhere:

```json
{ "command": "curl -s -d \"$KOFFIEPAUZE_MESSAGE\" ntfy.sh/your-topic" }
```

`KOFFIEPAUZE_OFF=1` disables the hooks for a session without uninstalling.

## What it will not do

- **Claude Code only.** The idea is portable, the hooks are not. Anything that can report "started" and "finished" per session can drive it; see `cmd_hook`.
- **One machine.** State lives in `~/.koffiepauze`. Agents on a server do not count.
- **No window counting.** It knows about sessions that have sent at least one prompt since you last rebooted the state. A terminal you opened and never used is invisible, which is the behaviour you want.
- **It cannot make you stop.** It says your coffee is ready. The laptop is still yours to close.

## Safety

Hooks run on every prompt, so they must never get in the way:

- every path exits `0`. A `Stop` hook that exits `2` blocks the stop and the session keeps going, so that is specifically guarded against in the tests.
- the hooks print nothing on stdout. `UserPromptSubmit` stdout is injected into the model's context, and a break reminder does not belong in your conversation.
- nudges are delivered from a detached process, never inside the hook, because speaking takes seconds and the hook runs before your prompt does.
- errors are swallowed and written to `~/.koffiepauze/koffiepauze.log`.

## Tests

```bash
python3 -m unittest -v
```

19 tests, no dependencies, against a temporary state directory and a frozen clock. They cover the decision table, the hook contract above, and that installing does not touch hooks you already had.

## Why it is called that

Dutch for "coffee break". It started as a joke in a [LinkedIn post](https://www.linkedin.com/in/ralphoei/) about a mug of coffee that kept going cold.

## Licence

MIT, see `LICENSE`.
