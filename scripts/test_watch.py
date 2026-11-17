"""Regression checks using isolated synthetic transcripts, never real sessions."""
import json
import select
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).with_name('watch.py')


def event(i, **extra):
    return dict(type='system', subtype='stop_hook_summary', uuid=str(i),
                timestamp=f'{i:04}', **extra)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.cursor = self.root / 'cursor.json'
        self.file = self.logs / 'session.jsonl'

    def run_watch(self, mode):
        result = subprocess.run([sys.executable, str(SCRIPT), mode, '--logs',
                                 str(self.logs), '--state', str(self.cursor),
                                 '--seconds', '0'], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_partial_fork_truncate_and_filters(self):
        self.file.write_text(json.dumps(event(1)) + '\n')
        self.assertEqual(self.run_watch('init')['latest'][0]['id'], '1')
        with self.file.open('a') as f:
            f.write(json.dumps(event(2)))
        self.assertEqual(self.run_watch('poll')['status'], 'waiting')
        with self.file.open('a') as f:
            f.write('\n' + json.dumps(event(3, isSidechain=True)) + '\n')
            f.write(json.dumps(event(4, preventedContinuation=True)) + '\n')
        self.assertEqual([e['id'] for e in self.run_watch('poll')['events']], ['2'])
        (self.logs / 'fork.jsonl').write_text(self.file.read_text() + json.dumps(event(5)) + '\n')
        self.assertEqual([e['id'] for e in self.run_watch('poll')['events']], ['5'])
        self.file.write_text(json.dumps(event(6)) + '\n')
        self.assertEqual([e['id'] for e in self.run_watch('poll')['events']], ['6'])

    def test_burst_is_drained_across_restarts(self):
        self.run_watch('init')
        self.file.write_text(''.join(json.dumps(event(i)) + '\n' for i in range(100)))
        seen = []
        for _ in range(50):
            result = self.run_watch('poll')
            self.assertEqual(len(result['events']), 2)
            seen.extend(e['id'] for e in result['events'])
        self.assertEqual(len(set(seen)), 100)
        self.assertEqual(self.run_watch('poll')['status'], 'waiting')

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_events_append_new_file_and_replace(self):
        self.file.write_text(json.dumps(event(1)) + '\n')
        self.run_watch('init')
        for kind, number in [('append', 2), ('new', 3), ('replace', 4)]:
            proc = subprocess.Popen(
                [sys.executable, str(SCRIPT), 'wait', '--logs', str(self.logs),
                 '--state', str(self.cursor), '--backend', 'events', '--seconds', '5'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.2)
                if kind == 'append':
                    with self.file.open('a') as f:
                        f.write(json.dumps(event(number)) + '\n')
                elif kind == 'new':
                    (self.logs / 'fork.jsonl').write_text(
                        self.file.read_text() + json.dumps(event(number)) + '\n')
                else:
                    temp = self.root / 'replacement'
                    temp.write_text(json.dumps(event(number)) + '\n')
                    temp.replace(self.file)
                out, err = proc.communicate(timeout=3)
                self.assertEqual(proc.returncode, 0, err)
                result = json.loads(out)
                self.assertEqual(result['backend'], 'kqueue')
                self.assertEqual([e['id'] for e in result['events']], [str(number)])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_idle_events_does_not_periodically_rescan(self):
        self.run_watch('init')
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'wait', '--logs', str(self.logs),
             '--state', str(self.cursor), '--backend', 'events', '--seconds', '5'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.3)
            stamp = self.cursor.stat().st_mtime_ns
            time.sleep(2.3)
            self.assertEqual(self.cursor.stat().st_mtime_ns, stamp)
            self.file.write_text(json.dumps(event(1)) + '\n')
            out, err = proc.communicate(timeout=2)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(json.loads(out)['backend'], 'kqueue')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


if __name__ == '__main__':
    unittest.main()
