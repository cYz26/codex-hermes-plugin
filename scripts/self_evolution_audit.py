#!/usr/bin/env python3
"""Export local audit evidence without starting Hermes or writing knowledge."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hermes_codex_learning.daily_audit import export, mark_reviewed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--timezone', default='Asia/Shanghai')
    parser.add_argument('--audit-chat', action='append', default=[])
    parser.add_argument('--acceptance-chat', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--review-note', type=Path)
    parser.add_argument('--snapshot-sha256')
    args = parser.parse_args()
    if args.review_note:
        if not args.snapshot_sha256:
            parser.error('--review-note requires --snapshot-sha256')
        result = mark_reviewed(args.out, args.date, args.review_note, args.snapshot_sha256)
    else:
        if not 0 < args.timeout <= 600:
            parser.error('--timeout must be in (0, 600] seconds')
        report = export(args.home, args.out, args.date, args.timezone, timeout=args.timeout,
                        audit_chats=args.audit_chat, acceptance_chats=args.acceptance_chat)
        result = {key: report[key] for key in ('date', 'state', 'semantic_review', 'collection', 'gaps')}
        result['report'] = str(args.out.resolve() / (report['date'] + '.md'))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
