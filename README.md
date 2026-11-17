# agent-session-review

An agent skill that lets one coding agent **watch another agent's local session and review each
completed turn** against evidence: files, diffs, test output and tool results.

It works in all four directions:

| Observer ↓ / Target → | Claude Code | Codex CLI |
| --------------------- | ----------- | --------- |
| Claude Code           | ✅          | ✅        |
| Codex CLI             | ✅          | ✅        |

The observer stays read-only. It does not take over the work, operate the target's device or
message the target unless you ask it to. Transcript text is treated as evidence, never as
instructions.

## What makes it different

- **Turn-completion based.** It wakes on real completion markers, not on text that looks final.
  For Claude Code that is a top-level `system/stop_hook_summary`. For Codex it is an `event_msg`
  with `task_complete` and a `turn_id`.
- **Quiet while idle.** `serve` is a single long-lived process that prints nothing until a turn
  completes. On macOS/BSD it blocks on kqueue file events. Elsewhere it falls back to polling
  every 2 seconds and says so once.
- **Resumable private cursor.** Offsets and seen IDs persist outside the watched logs. Forked or
  resumed sessions, copied history, partial lines and file replacement or truncation are handled
  without re-reporting old turns. A lock stops two watchers from consuming the same cursor.
- **Safe previews.** Thinking, encrypted reasoning, images and base64 blobs are left out of
  previews. Text is capped in size.
- **Verification-first review.** The skill tells the observer to check claims against files and
  tests, and to correct unsupported causal claims. A setting being present doesn't mean it's
  active, and API success doesn't mean end-to-end success.

## Requirements

- Python 3.9+ with the standard library only. Tested on macOS (3.14) and Linux (3.9, 3.12).
- macOS or Linux. It uses `fcntl`, so native Windows isn't supported. On Linux, kernel events
  aren't used, so `--backend auto` polls every 2 seconds.
- Read access to `~/.claude/projects` (Claude Code) and/or `~/.codex/sessions` (Codex).

## Install

Clone into your agent's skills directory:

```sh
# Claude Code
git clone https://github.com/freeman106/agent-session-review ~/.claude/skills/agent-session-review

# Codex CLI
git clone https://github.com/freeman106/agent-session-review ~/.codex/skills/agent-session-review
```

Then ask your agent, for example:

> Use agent-session-review to watch my Codex session in /path/to/project and review each
> completed turn.

In Claude Code you can also run `/agent-session-review …`. In Codex use `$agent-session-review …`.

## Helper CLI

The skill drives `scripts/watch.py`. You can also run it directly:

```sh
# Find candidate sessions for a project
python3 scripts/watch.py discover --agent codex --cwd /abs/project

# Create a cursor once (marks existing completions as seen and previews the latest)
python3 scripts/watch.py init  --agent codex --logs /abs/rollout-XXXX.jsonl --state /tmp/review.json

# Long-lived, silent watcher: one JSON line per batch of completed turns
python3 -u scripts/watch.py serve --agent codex --logs /abs/rollout-XXXX.jsonl --state /tmp/review.json

# One-shot alternatives
python3 scripts/watch.py wait --seconds 50 ...   # block up to 50 s
python3 scripts/watch.py poll ...                # drain queued completions now
```

For Claude Code targets, pass `--agent claude` and the project directory under
`~/.claude/projects/` as `--logs`. Use `--exclude-session <id>` to skip the observer's own
session. Subagents, guardians and sidechains are excluded unless you pass
`--include-subagents`.

Each output item holds at most two completions. Remaining ones stay queued in the cursor, and
`pending_count` tells you how many are left.

## Limitations

- This is active-turn observation. A finished observer turn isn't woken up unless the host has a
  wake-on-output monitor, such as Claude Code's `Monitor`. There's no daemon, hook or LaunchAgent.
- The parsers follow the transcript formats Claude Code and Codex used as of late 2026. Those
  formats aren't public APIs and can change.
- Compressed `.zst` archives aren't watched.

## Tests

The tests use synthetic transcripts only, never real sessions:

```sh
cd scripts && python3 -m unittest -v test_watch test_codex_and_stream
```

Tests that need kqueue are skipped on platforms without it.

## License

[MIT](LICENSE)
