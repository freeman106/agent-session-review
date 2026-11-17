#!/usr/bin/env python3
"""Read-only Claude/Codex completion watcher with a private, resumable cursor."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import tempfile
import time


def paths_in(target):
    return [target] if target.is_file() else sorted(target.glob('*.jsonl'))


class ChangeWaiter:
    def __init__(self, target, backend='auto'):
        self.target, self.queue, self.handles, self.reason = target, None, {}, None
        if backend != 'poll':
            if hasattr(select, 'kqueue'):
                try:
                    self.queue = select.kqueue()
                    self.sync()
                except OSError as exc:
                    self.close()
                    if backend == 'events':
                        raise
                    self.reason = str(exc)
            elif backend == 'events':
                raise RuntimeError('kqueue is unavailable on this platform')
            else:
                self.reason = 'kqueue is unavailable on this platform'
        self.backend = 'kqueue' if self.queue is not None else 'poll'

    def sync(self):
        if self.queue is None:
            return
        directory = self.target.parent if self.target.suffix == '.jsonl' else self.target
        paths = {directory, *paths_in(self.target)}
        for path, (fd, identity) in list(self.handles.items()):
            try:
                stat = path.stat()
                current = (stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                current = None
            if path not in paths or current != identity:
                os.close(fd)
                del self.handles[path]
        for path in paths:
            if path in self.handles:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                stat = os.fstat(fd)
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB |
                            select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE))
                self.queue.control([event], 0, 0)
                self.handles[path] = (fd, (stat.st_dev, stat.st_ino))
            except BaseException:
                os.close(fd)
                raise

    def wait(self, seconds):
        if self.queue is None:
            time.sleep(min(2, seconds))
        else:
            self.queue.control(None, max(1, len(self.handles)), seconds)

    def close(self):
        for fd, _ in self.handles.values():
            os.close(fd)
        self.handles.clear()
        if self.queue is not None:
            self.queue.close()
            self.queue = None


def read_rows(path, offset=0):
    rows = []
    with path.open('rb') as f:
        f.seek(offset)
        while True:
            start = f.tell()
            line = f.readline()
            if not line or not line.endswith(b'\n'):
                return rows, start
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except (ValueError, UnicodeError):
                pass


def metadata(row, old):
    meta = dict(old)
    if row.get('type') == 'session_meta':
        payload = row.get('payload', {})
        if isinstance(payload, dict):
            meta.update(agent='codex', session=payload.get('id', payload.get('session_id')),
                        cwd=payload.get('cwd'), subagent=isinstance(payload.get('source'), dict)
                        and 'subagent' in payload['source'])
    elif row.get('type') in ('user', 'assistant', 'system'):
        meta.setdefault('agent', 'claude')
        for source, dest in [('sessionId', 'session'), ('cwd', 'cwd')]:
            if row.get(source):
                meta[dest] = row[source]
    return meta


def allowed(meta, row, args):
    if row.get('isSidechain') and not args.include_subagents:
        return False
    if meta.get('subagent') and not args.include_subagents:
        return False
    if args.agent != 'auto' and meta.get('agent') != args.agent:
        return False
    session = meta.get('session')
    return (not args.session or session in args.session) and session not in args.exclude_session


def completion(row):
    if (row.get('type') == 'system' and row.get('subtype') == 'stop_hook_summary'
            and not row.get('preventedContinuation') and row.get('uuid')):
        return 'claude', row['uuid']
    payload = row.get('payload', {})
    if (row.get('type') == 'event_msg' and isinstance(payload, dict)
            and payload.get('type') == 'task_complete' and payload.get('turn_id')):
        return 'codex', 'codex:' + str(payload['turn_id'])
    return None


def text_entry(row):
    kind, timestamp = row.get('type'), row.get('timestamp')
    if kind in ('user', 'assistant'):
        message = row.get('message', {})
        role = kind
    elif kind == 'response_item' and isinstance(row.get('payload'), dict):
        message = row['payload']
        if message.get('type') != 'message':
            return None
        role = message.get('role')
        if role not in ('user', 'assistant'):
            return None
    else:
        return None
    if not isinstance(message, dict):
        return None
    blocks = message.get('content', [])
    if isinstance(blocks, str):
        blocks = [{'type': 'text', 'text': blocks}]
    texts = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get('type') in ('text', 'input_text', 'output_text'):
            texts.append(str(block.get('text', ''))[:3000])
        elif role == 'assistant' and block.get('type') == 'tool_use':
            texts.append('[tool] ' + str(block.get('name', '')))
    if texts:
        return dict(type=role, time=timestamp, text='\n'.join(texts)[:4000])
    return None


def preview(path, event_id, args):
    entries, meta = [], {}
    with path.open('rb') as f:
        for line in f:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            meta = metadata(row, meta)
            mark = completion(row)
            if mark and mark[1] == event_id:
                payload = row.get('payload', {})
                if not any(e['type'] == 'assistant' for e in entries) and mark[0] == 'codex':
                    text = payload.get('last_agent_message', '')
                    if isinstance(text, str) and text:
                        entries.append(dict(type='assistant', time=row.get('timestamp'), text=text[:4000]))
                break
            if not allowed(meta, row, args):
                continue
            payload = row.get('payload', {})
            if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                entries = []
            entry = text_entry(row)
            if entry:
                if entry['type'] == 'user':
                    entries = []
                entries.append(entry)
                entries = entries[-12:]
    out, budget = [], 12000
    for entry in reversed(entries):
        if budget <= 0:
            break
        entry['text'] = entry['text'][:budget]
        budget -= len(entry['text'])
        out.append(entry)
    return list(reversed(out))


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.review-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def scan(target, state, args):
    seen, events = set(state['seen']), []
    for path in paths_in(target):
        try:
            stat = path.stat()
            old = state['files'].get(path.name, {})
            identity = [stat.st_dev, stat.st_ino]
            offset = old.get('offset', 0)
            if old.get('identity') != identity or stat.st_size < offset:
                offset, old = 0, {}
            rows, end = read_rows(path, offset)
        except FileNotFoundError:
            continue
        meta = old.get('meta', {})
        for row in rows:
            meta = metadata(row, meta)
            mark = completion(row)
            if mark and allowed(meta, row, args) and mark[1] not in seen:
                seen.add(mark[1])
                events.append(dict(file=str(path), agent=mark[0], session=meta.get('session'),
                    id=mark[1], time=row.get('timestamp'), context=preview(path, mark[1], args)))
        state['files'][path.name] = dict(identity=identity, offset=end, meta=meta)
    state['seen'] = sorted(seen)
    return sorted(events, key=lambda e: (e['time'] or '', e['id']))


def discover(args):
    roots = [(args.agent, args.logs)] if args.logs else [
        ('claude', Path.home() / '.claude/projects'), ('codex', Path.home() / '.codex/sessions')]
    candidates = []
    for agent, root in roots:
        if args.agent != 'auto' and agent != args.agent:
            continue
        if not root.exists():
            continue
        files = [root] if root.is_file() else root.rglob('*.jsonl')
        for path in files:
            try:
                # Metadata is near the start; do not read whole transcripts for discovery.
                meta = {}
                with path.open('rb') as f:
                    for _ in range(24):
                        line = f.readline(1024 * 1024)
                        if not line:
                            break
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(row, dict):
                            meta = metadata(row, meta)
                            if row.get('isSidechain'):
                                meta['subagent'] = True
                        if meta.get('cwd') and meta.get('session'):
                            break
                if not meta.get('agent') or not allowed(meta, {}, args):
                    continue
                if args.cwd:
                    cwd = Path(meta.get('cwd') or '/').resolve()
                    wanted = args.cwd.expanduser().resolve()
                    if cwd != wanted and wanted not in cwd.parents:
                        continue
                candidates.append(dict(file=str(path), **meta, modified=path.stat().st_mtime))
            except (OSError, ValueError):
                continue
    return dict(status='candidates', sessions=sorted(candidates,
                key=lambda x: x['modified'], reverse=True)[:args.limit])


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['discover', 'init', 'wait', 'poll', 'serve'])
    parser.add_argument('--logs', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--agent', choices=['claude', 'codex', 'auto'], default='claude')
    parser.add_argument('--session', action='append', default=[])
    parser.add_argument('--exclude-session', action='append', default=[])
    parser.add_argument('--include-subagents', action='store_true')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--seconds', type=float, default=50)
    parser.add_argument('--backend', choices=['auto', 'events', 'poll'], default='auto')
    args = parser.parse_args()
    if args.mode == 'discover':
        emit(discover(args))
        return
    if not args.logs or not args.state or not 0 <= args.seconds <= 50:
        parser.error('logs and state required; seconds must be between 0 and 50')
    logs, cursor = args.logs.expanduser().resolve(), args.state.expanduser().resolve()
    if not logs.exists() or (logs.is_file() and logs.suffix != '.jsonl'):
        parser.error('target must be an existing JSONL file or its immediate directory')
    if cursor == logs or (logs.is_dir() and logs in cursor.parents):
        parser.error('state must be outside the watched directory')
    cursor.parent.mkdir(parents=True, exist_ok=True)
    lockfd = os.open(str(cursor) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('a watcher already owns this cursor')
        os.ftruncate(lockfd, 0)
        os.write(lockfd, str(os.getpid()).encode())
        if args.mode == 'init':
            if cursor.exists():
                parser.error('state already exists; use serve or wait to resume')
            state = dict(version=2, logs=str(logs), agent=args.agent, files={}, seen=[], pending=[])
            events = scan(logs, state, args)
            save(cursor, state)
            emit(dict(status='initialized', latest=events[-2:]))
            return
        if not cursor.exists():
            parser.error('initialize the watcher first')
        state = json.loads(cursor.read_text())
        if state.get('version') not in (1, 2) or state.get('logs') != str(logs):
            parser.error('state belongs to a different target or version')
        if state.get('agent', 'claude') != args.agent:
            parser.error('agent selection changed; use a separate cursor')
        state.update(version=2, agent=args.agent)
        waiter = ChangeWaiter(logs, args.backend)
        def stop(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        deadline = time.monotonic() + (0 if args.mode == 'poll' else args.seconds)
        try:
            if waiter.reason and args.mode == 'serve':
                emit(dict(status='backend_fallback', backend=waiter.backend, reason=waiter.reason))
            while True:
                waiter.sync()
                before = json.dumps(state, sort_keys=True)
                pending = state.setdefault('pending', [])
                pending.extend(scan(logs, state, args))
                events, state['pending'] = pending[:2], pending[2:]
                if json.dumps(state, sort_keys=True) != before:
                    save(cursor, state)
                result = dict(backend=waiter.backend, fallback_reason=waiter.reason)
                if events:
                    emit(dict(result, status='completed_turns', events=events,
                              pending_count=len(state['pending'])))
                    if args.mode != 'serve':
                        return
                    continue
                remaining = deadline - time.monotonic()
                if args.mode != 'serve' and remaining <= 0:
                    emit(dict(result, status='waiting'))
                    return
                waiter.wait(50 if args.mode == 'serve' else remaining)
        finally:
            waiter.close()
    finally:
        os.close(lockfd)


if __name__ == '__main__':
    main()
