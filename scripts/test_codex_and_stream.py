"""Black-box checks for Codex parsing and a quiet, reusable watcher process."""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).with_name('watch.py')


def meta(session='target', source='cli'):
    return dict(type='session_meta', payload=dict(id=session, cwd='/project', source=source))


def codex_done(turn):
    return dict(type='event_msg', timestamp='2026-11-16T00:00:00Z',
                payload=dict(type='task_complete', turn_id=turn, last_agent_message='Finished'))


class ExtendedWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.cursor = self.root / 'cursor.json'
        self.file = self.logs / 'rollout.jsonl'

    def append(self, *rows, path=None):
        with (path or self.file).open('a') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')

    def args(self, mode, agent='codex', logs=None):
        return [sys.executable, str(SCRIPT), mode, '--logs', str(logs or self.logs),
                '--state', str(self.cursor), '--agent', agent]

    def run_watch(self, mode, agent='codex', logs=None, extra=()):
        r = subprocess.run(self.args(mode, agent, logs) + ['--seconds', '0', *extra],
                           capture_output=True, text=True, timeout=10, check=True)
        return json.loads(r.stdout)

    def test_codex_completion_requires_task_complete(self):
        self.append(meta())
        self.run_watch('init')
        self.append(dict(type='response_item', payload=dict(type='message', role='assistant',
                    content=[dict(type='output_text', text='Final-looking text'),
                             dict(type='image', data='SECRET_IMAGE')])),
                    dict(type='response_item', payload=dict(type='reasoning', encrypted_content='SECRET')))
        self.assertEqual(self.run_watch('wait')['status'], 'waiting')
        self.append(codex_done('turn-1'))
        result = self.run_watch('wait')
        self.assertEqual(result['events'][0]['id'], 'codex:turn-1')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIn('Final-looking text', json.dumps(result))

    def test_codex_subagents_and_observer_are_excluded(self):
        self.append(meta(source={'subagent': {'other': 'guardian'}}), codex_done('child'))
        self.assertEqual(self.run_watch('init')['latest'], [])
        own = self.logs / 'own.jsonl'
        self.append(meta('observer'), codex_done('own-turn'), path=own)
        result = self.run_watch('wait', extra=['--exclude-session', 'observer'])
        self.assertEqual(result['status'], 'waiting')
        self.append(meta('root'), codex_done('root-turn'), path=self.logs / 'root.jsonl')
        self.assertEqual(self.run_watch('wait', extra=['--exclude-session', 'observer'])['events'][0]['id'],
                         'codex:root-turn')

    def test_codex_fork_deduplicates_copied_turn(self):
        self.append(meta(), codex_done('copied'))
        self.run_watch('init')
        self.append(meta('fork'), codex_done('copied'), codex_done('new'),
                    path=self.logs / 'fork.jsonl')
        self.assertEqual([e['id'] for e in self.run_watch('wait')['events']], ['codex:new'])

    def test_exact_file_target(self):
        self.append(meta())
        self.run_watch('init', logs=self.file)
        self.append(codex_done('selected'))
        self.append(meta('other'), codex_done('unrelated'), path=self.logs / 'other.jsonl')
        self.assertEqual([e['id'] for e in self.run_watch('wait', logs=self.file)['events']],
                         ['codex:selected'])

    def test_legacy_claude_cursor_resumes_without_reinitializing(self):
        row = dict(type='system', subtype='stop_hook_summary', uuid='old')
        self.append(row)
        stat = self.file.stat()
        self.cursor.write_text(json.dumps(dict(version=1, logs=str(self.logs.resolve()), seen=['old'],
            files={self.file.name: dict(identity=[stat.st_dev, stat.st_ino], offset=stat.st_size)})))
        self.append(dict(row, uuid='new'))
        self.assertEqual([e['id'] for e in self.run_watch('wait', agent='claude')['events']], ['new'])

    def test_mixed_sources_keep_agent_identity(self):
        self.append(meta(), codex_done('codex-turn'))
        self.append(dict(type='system', subtype='stop_hook_summary', uuid='claude-turn'),
                    path=self.logs / 'claude.jsonl')
        result = self.run_watch('init', agent='auto')
        self.assertEqual({e['agent'] for e in result['latest']}, {'claude', 'codex'})

    def test_discovery_excludes_claude_sidechain_unless_requested(self):
        self.append(dict(type='user', sessionId='child', cwd='/project', isSidechain=True,
                         message=dict(content='child prompt')))
        args = self.args('discover', agent='claude')
        result = json.loads(subprocess.check_output(args, text=True))
        self.assertEqual(result['sessions'], [])
        result = json.loads(subprocess.check_output(args + ['--include-subagents'], text=True))
        self.assertEqual(result['sessions'][0]['session'], 'child')

    def test_poll_does_not_wait_for_its_default_timeout(self):
        self.append(meta())
        self.run_watch('init')
        result = subprocess.run(self.args('poll'), capture_output=True, text=True,
                                timeout=1.5, check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'waiting')

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'Requires macOS/BSD kernel events')
    def test_serve_is_silent_and_emits_two_completions_without_restarting(self):
        self.append(meta())
        self.run_watch('init')
        proc = subprocess.Popen(self.args('serve') + ['--backend', 'events'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.3)
            self.assertIsNone(proc.poll())
            self.assertFalse(select.select([proc.stdout], [], [], 0)[0])
            for turn in ['one', 'two']:
                self.append(codex_done(turn))
                self.assertTrue(select.select([proc.stdout], [], [], 3)[0])
                event = json.loads(proc.stdout.readline())
                self.assertEqual(event['events'][0]['id'], 'codex:' + turn)
                self.assertIsNone(proc.poll())
                self.assertFalse(select.select([proc.stdout], [], [], 0.15)[0])
            proc.terminate()
            out, err = proc.communicate(timeout=3)
            self.assertEqual((proc.returncode, out, err), (0, '', ''))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'Requires macOS/BSD kernel events')
    def test_second_watcher_cannot_consume_the_same_cursor(self):
        self.append(meta())
        self.run_watch('init')
        proc = subprocess.Popen(self.args('serve') + ['--backend', 'events'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.3)
            second = subprocess.run(self.args('wait'), capture_output=True, text=True, timeout=3)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('already', second.stderr.lower())
        finally:
            proc.terminate()
            proc.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
