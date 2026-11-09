"""Metadata-only audit of a synthetic desktop notice against an idle acceptance chat."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_codex_learning.config import Settings
from hermes_codex_learning.notices import NoticeQueue
from hermes_codex_learning.store import atomic_json


def snapshot(path, chat, marker):
    root = (Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions').resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_uid != os.getuid():
        raise ValueError('Untrusted host transcript')
    raw = path.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Transcript exceeds bounded audit size')
    rows = [json.loads(line) for line in raw.splitlines()]
    if rows[0].get('type') != 'session_meta' or rows[0]['payload'].get('id') != chat:
        raise ValueError('Transcript belongs to a different chat')
    counts = Counter(row['type'] + ':' + str(row.get('payload', {}).get('type', '')) for row in rows)
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'rows': len(rows),
            'counts': dict(counts), 'notice_marker_present': marker.encode() in raw, 'at': time.time()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'verify'])
    parser.add_argument('--settings', required=True)
    parser.add_argument('--chat', required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.load(args.settings)
    record_path = args.workspace / 'audit.json'
    queue = NoticeQueue(settings.state)
    try:
        if args.action == 'prepare':
            args.workspace.mkdir(parents=True, mode=0o700)
            db = sqlite3.connect('file:' + str(settings.state / 'bridge.sqlite') + '?mode=ro', uri=True)
            try:
                row = db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.session_id')=? "
                                 "AND json_extract(payload,'$.transcript_path') IS NOT NULL "
                                 "ORDER BY created DESC LIMIT 1", (args.chat,)).fetchone()
            finally:
                db.close()
            payload = json.loads(row[0])
            marker, identifier = uuid.uuid4().hex, 'notice-isolation-' + uuid.uuid4().hex
            record = {'chat': args.chat, 'path': payload['transcript_path'], 'scope': payload['bridge_project'],
                      'marker': marker, 'notice': identifier, 'synthetic': True}
            record['before'] = snapshot(Path(record['path']), args.chat, marker)
            atomic_json(record_path, record)
            queue.publish({'id': identifier, 'kind': 'publication.pending', 'project': record['scope'],
                           'name': 'notification-isolation-fixture',
                           'message': 'Hermes 合成通知隔离验收（无实际学习改动）',
                           'receipt': '这是通知通道的合成验收。仅打开本窗口即可，无需复制正文到聊天。\n' + marker})
            print(json.dumps({'state': 'queued', 'audit': str(record_path)}))
        else:
            record = json.loads(record_path.read_text())
            if record['chat'] != args.chat:
                raise ValueError('Audit chat mismatch')
            after = snapshot(Path(record['path']), args.chat, record['marker'])
            stages = [row[0] for row in queue.db.execute('SELECT DISTINCT stage FROM deliveries WHERE id=?', (record['notice'],))]
            proof = {'synthetic': True, 'chat': args.chat, 'stages': stages,
                     'transcript_unchanged': after['sha256'] == record['before']['sha256'],
                     'notice_marker_absent': not after['notice_marker_present'],
                     'usage_records_unchanged': after['counts'].get('token_usage_record:', 0) ==
                                               record['before']['counts'].get('token_usage_record:', 0),
                     'new_turns': after['counts'].get('event_msg:task_started', 0) -
                                  record['before']['counts'].get('event_msg:task_started', 0),
                     'actual_compaction_verified': False, 'checked_at': time.time()}
            proof['passed_idle_delivery_and_open'] = ('read' in stages and proof['transcript_unchanged']
                                                     and proof['usage_records_unchanged'] and proof['notice_marker_absent'])
            atomic_json(args.workspace / 'result.json', proof)
            print(json.dumps(proof))
    finally:
        queue.close()


if __name__ == '__main__':
    main()
