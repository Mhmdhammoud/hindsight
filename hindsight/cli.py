"""hindsight <command> <client> ...  (the deterministic steps the hindsight-sync skill calls)"""
import argparse
import json
import re
import sys
from pathlib import Path

from . import core


def cmd_pending(a):
    """New meetings for the client from GRANOLA_MCP_LIST_MEETINGS results: saved files, or (--from-log)
    every list result in the last N hours of session data."""
    cfg, have, seen, out = core.config(a.client), core.stored_ids(a.client), set(), []
    fetched = core.stored_ids(a.client, 'transcripts')
    texts = list(_session_results('<meetings_data', a.hours * 3600)) if a.from_log else [Path(f).read_text() for f in a.files]
    for text in texts:
        for m in core.parse_meeting_list(text):
            why = core.matches(cfg, m['title'], m['emails'], m['id'])
            if why and m['id'] not in have and m['id'] not in seen:
                seen.add(m['id'])
                out.append({'id': m['id'], 'title': m['title'], 'at': m['at'], 'why': why,
                            'fetched': m['id'] in fetched})
    print(json.dumps(sorted(out, key=lambda m: m['at'] or ''), indent=1))


def cmd_normalize(a):
    """Save transcripts from GET_MEETING_TRANSCRIPT results: files the harness saved, or (--from-log)
    meeting ids whose result is looked up byte-exact in recent Claude Code session logs."""
    bad = 0
    for src in a.files:
        try:
            raw = _from_log(src) if a.from_log else Path(src).read_text()
            print(json.dumps(core.save_transcript(a.client, raw)))
        except Exception as e:  # keep going; report every failure
            bad += 1
            print(json.dumps({'source': src, 'error': str(e)}))
    return 1 if bad else 0


def _session_results(hint: str, max_age_s: float):
    """Exact MCP tool-result texts containing `hint` from recent Claude Code session data, newest file first:
    inline results in session/subagent *.jsonl, and results the harness spilled to */tool-results/*.txt.
    This is how the skill hands results to Python without anyone copying text."""
    import time
    files = [p for root in Path.home().glob('.claude*/projects')
             for pat in ('*.jsonl', 'tool-results/*.txt', 'tool-results/*.json')
             for p in root.rglob(pat) if time.time() - p.stat().st_mtime < max_age_s]
    for f in sorted(files, key=lambda p: -p.stat().st_mtime):
        if f.suffix in ('.txt', '.json'):
            s = f.read_text(errors='replace')
            if hint not in s:
                continue
            if f.suffix == '.json':  # persisted output: [{"type": "text", "text": ...}]
                try:
                    yield from (b.get('text', '') for b in json.loads(s) if hint in b.get('text', ''))
                except (ValueError, AttributeError, TypeError):
                    pass
            else:
                yield s
            continue
        found = []
        with open(f, errors='replace') as fh:
            for line in fh:
                if hint not in line or 'tool_result' not in line:
                    continue
                for block in json.loads(line).get('message', {}).get('content', []):
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        c = block['content']
                        found += [t for t in ([x.get('text', '') for x in c] if isinstance(c, list) else [c]) if hint in t]
        yield from reversed(found)


def _from_log(mid: str) -> str:
    """The newest GET_MEETING_TRANSCRIPT result for this meeting in the last 2 days of session data."""
    needle = f'\\"id\\": \\"{mid}\\"'
    for t in _session_results(mid, 2 * 86400):
        if needle in t and '\\"transcript\\"' in t:
            return t
    raise LookupError(f'no GET_MEETING_TRANSCRIPT result for {mid} in recent session data')


def cmd_verify(a):
    """Mark quotes verified: true/false and detect duplicate recordings of the same call."""
    print(json.dumps({**core.verify(a.client), 'duplicates': len(core.find_duplicates(a.client))}))


def cmd_unverified(a):
    """JSON list of items whose quote does not verify, with the closest transcript span (read-only)."""
    print(json.dumps(core.unverified(a.client), indent=1, ensure_ascii=False))


def cmd_digest(a):
    """Write <client>/digest.md (date order, unverified flagged) for the reconcile step; --new = not yet reconciled."""
    p = core.client_dir(a.client) / 'digest.md'
    text = core.digest(a.client, a.new)
    p.write_text(text)
    print(json.dumps({'path': str(p), 'meetings': text.count('\n## ') + text.startswith('## ')}))


def cmd_reconciled(a):
    """Record every stored meeting as folded into the docs (run after a successful reconcile); checks superseded.json refs."""
    refs = {i['ref'] for i in core.items(a.client)}
    bad = sorted(set(core.superseded(a.client)) - refs)
    print(json.dumps({'reconciled': core.mark_reconciled(a.client), 'superseded': len(core.superseded(a.client)),
                      'unknown_refs': bad}))
    return 1 if bad else 0


def cmd_embed(a):
    from . import gemini
    print(json.dumps(core.embed_new(a.client, gemini.embed)))


def cmd_search(a):
    from . import gemini
    for r in core.search(a.client, a.question, a.k, gemini.embed):
        flag = '' if r['verified'] else ' [unverified]'
        print(f"{r['date']} {r['meeting_id'][:8]} {r['match'][:3]} {r['text'][:110]}{flag}")


def cmd_eval(a):
    """Port of research/compare.py scoring: a question scores if a top-K item comes from a meeting whose
    transcript has a 300-word window (stride 240) matching every answer regex. Prints hits and MRR."""
    from . import gemini
    d = core.client_dir(a.client)
    qs = json.loads((d / 'eval' / 'questions.json').read_text())
    words = {p.stem: p.read_text().split('\n', 1)[-1].split() for p in (d / 'transcripts').glob('*.txt')}

    def gold(q):
        return {mid for mid, w in words.items()
                if any(all(re.search(p, ' '.join(w[s:s + 300]), re.I) for p in q['answer']) for s in range(0, len(w), 240))}

    hits = []
    for q in qs:
        g = gold(q)
        top = core.search(a.client, q['q'], a.k, gemini.embed)
        rank = next((n + 1 for n, r in enumerate(top) if r['match'] == 'semantic' and r['meeting_id'] in g), None)
        hits.append(rank)
        print(f"{'#' + str(rank) if rank else ' -':>4}  {q['q']}")
    found = sum(h is not None for h in hits)
    print(f"\n{found}/{len(qs)} in top-{a.k}  (MRR {sum(1 / h for h in hits if h) / len(qs):.2f})")
    return 0 if found >= a.min else 1


def cmd_extract(a):
    """Extract meetings with Claude (default: every transcript that has no extract yet)."""
    from . import pipeline
    ids = a.ids or sorted(core.stored_ids(a.client, 'transcripts') - core.stored_ids(a.client))
    bad = 0
    for mid in ids:
        try:
            print(json.dumps(pipeline.extract(a.client, mid, a.model)))
        except Exception as e:
            bad += 1
            print(json.dumps({'id': mid, 'error': str(e)[:300]}))
    return 1 if bad else 0


def cmd_reconcile(a):
    """Rebuild brief/commitments/changes/superseded with Claude and mark meetings reconciled."""
    from . import pipeline
    print(json.dumps(pipeline.reconcile(a.client, a.full, a.model), indent=1))


def cmd_sync(a):
    """Fetch new Granola notes (API), extract, verify, embed and reconcile, for the given clients or all of them."""
    from . import pipeline
    clients = a.clients or [c['client'] for c in core.list_clients()]
    failed = False
    if not a.no_fetch:
        try:
            for c, saved in pipeline.fetch(clients, a.since).items():
                failed |= any('error' in x for x in saved)
                print(json.dumps({'client': c, 'fetched': saved}))
        except Exception as e:  # still extract/reconcile whatever is on disk
            failed = True
            print(json.dumps({'fetch_error': str(e)[:300]}))
    for c in clients:
        try:
            r = pipeline.sync(c)
            failed |= bool(r['errors'])
            print(json.dumps(r))
        except Exception as e:
            failed = True
            print(json.dumps({'client': c, 'error': str(e)[:300]}))
    return 1 if failed else 0


def cmd_add_client(a):
    """Create <home>/clients/<client>/client.json. Meetings match by attendee email domain or title keyword."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', a.client):
        raise SystemExit('client must be lowercase letters, digits, - or _')
    d = core.home() / 'clients' / a.client
    if (d / 'client.json').exists():
        raise SystemExit(f'{d}/client.json already exists')
    d.mkdir(parents=True, exist_ok=True)
    cfg = {'name': a.name or a.client, 'domains': a.domain, 'title_keywords': a.keyword}
    (d / 'client.json').write_text(json.dumps(cfg, indent=2) + '\n')
    print(json.dumps({'created': str(d / 'client.json'), **cfg}))


def cmd_doctor(a):
    """Check everything sync needs: claude login, Gemini key + embeddings, Granola key, team.json."""
    import shutil
    import subprocess
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f'ok    {name}: {fn()}')
        except Exception as e:
            ok = False
            print(f'FAIL  {name}: {str(e)[:200]}')

    def claude():
        from . import pipeline
        if not shutil.which(pipeline.CLAUDE):
            raise RuntimeError(f'{pipeline.CLAUDE} not on PATH (https://claude.com/claude-code), then `claude /login`')
        pipeline.ask('Reply with ok=true.', {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                                             'required': ['ok']}, timeout=180)
        return f'{pipeline.CLAUDE} -p works ({pipeline.MODEL})'

    def embed():
        from . import gemini
        return f"{len(gemini.embed(['hello'], True)[0])} dims"

    def granola_key():
        from . import granola
        granola._key()
        granola._get('/notes', created_after='2100-01-01T00:00:00Z')
        return 'key works'

    def team():
        p = core.home() / 'team.json'
        if not p.exists():
            raise RuntimeError(f'missing {p}: {{"vendor": "Your company", "people": ["Your Name", ...]}} (or run hindsight setup)')
        return ', '.join(json.loads(p.read_text()).get('people', []))[:80]

    check('claude', claude)
    check('gemini embeddings', embed)
    check('granola api', granola_key)
    check('team.json', team)
    check('clients', lambda: ', '.join(c['client'] for c in core.list_clients()) or 'none yet: hindsight add-client')
    return 0 if ok else 1


KEYS = [  # env var, Keychain service, Keychain account, where to get it
    ('GRANOLA_API_KEY', 'hindsight-granola', 'granola',
     'Granola app > Settings > Connectors > API keys (Personal + Public scopes)'),
    ('GEMINI_API_KEY', 'hindsight-gemini', 'gemini', 'https://aistudio.google.com/apikey (semantic search)'),
]


def cmd_setup(a):
    """One-time setup, safe to re-run: data dir, team.json, API keys, Claude Code skills + MCP server, doctor."""
    import os
    import plistlib
    import shutil
    import subprocess
    from . import pipeline
    pkg, home, exe = Path(__file__).resolve().parent, core.home(), shutil.which('hindsight') or sys.argv[0]
    say = lambda s: print(f'\n== {s}')

    say(f'data: {home}')
    (home / 'clients').mkdir(parents=True, exist_ok=True)
    if not (home / 'team.json').exists():  # our people, so extraction can tell us apart from the client
        tty = sys.stdin.isatty()
        vendor = (tty and input('your company name: ').strip()) or 'us'
        people = (tty and input('your team, comma-separated (e.g. Mo, Dave Smith): ')) or ''
        (home / 'team.json').write_text(json.dumps(
            {'vendor': vendor, 'people': [p.strip() for p in people.split(',') if p.strip()]}))
        print(f'created {home}/team.json (edit it any time)')

    for env, service, account, where in KEYS:
        say(f'{env}: {where}')
        add = ['security', 'add-generic-password', '-U', '-s', service, '-a', account, '-w']
        if os.environ.get(env) or not subprocess.run(['security', 'find-generic-password', '-s', service],
                                                     capture_output=True).returncode:
            print('already set')
        elif sys.stdin.isatty():  # security prompts for the key itself: it never lands in argv or shell history
            print('paste the key when prompted (stored in your login Keychain)')
            subprocess.run(add)
        else:
            print(f'no terminal; run later: {" ".join(add)}  (or put {env}=... in ~/.hindsight/.env)')

    say('Claude Code skills + MCP server')
    if not shutil.which(pipeline.CLAUDE):
        print(f'{pipeline.CLAUDE} not on PATH: install Claude Code (https://claude.com/claude-code), `claude /login`, re-run')
    else:
        skills = Path.home() / '.claude' / 'skills'
        skills.mkdir(parents=True, exist_ok=True)
        for s in (pkg / 'skills').iterdir():
            dst = skills / s.name
            if dst.is_symlink():  # ours from an older install or checkout: repoint
                dst.unlink()
            if dst.exists():
                print(f'skipped {dst}: not a symlink, remove it to let setup manage it')
            else:
                dst.symlink_to(s)
                print(f'linked {dst}')
        if subprocess.run([pipeline.CLAUDE, 'mcp', 'get', 'hindsight'], capture_output=True).returncode:
            subprocess.run([pipeline.CLAUDE, 'mcp', 'add', '--scope', 'user', 'hindsight', '--', exe, 'serve'])
        else:
            print('hindsight MCP already registered')

    if a.schedule:
        say('launchd job: hindsight sync at 07:00 and 19:00')
        plist = Path.home() / 'Library/LaunchAgents/com.zaigo.hindsight-sync.plist'
        log = str(Path.home() / 'Library/Logs/hindsight-sync.log')
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({
            'Label': 'com.zaigo.hindsight-sync', 'ProgramArguments': [exe, 'sync'],
            'EnvironmentVariables': {'PATH': f'{Path(exe).parent}:{Path.home()}/.local/bin:'
                                             '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
            'StartCalendarInterval': [{'Hour': 7, 'Minute': 0}, {'Hour': 19, 'Minute': 0}],
            'StandardOutPath': log, 'StandardErrorPath': log}))
        uid = os.getuid()
        subprocess.run(['launchctl', 'bootout', f'gui/{uid}', str(plist)], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', str(plist)], check=True)
        print(f'installed {plist} (log: {log})')

    say('checks')
    rc = cmd_doctor(a)
    print('\nNext:\n  hindsight add-client acme --name "Acme Corp" --domain acme.com --keyword Acme\n'
          '  hindsight sync acme --since 2026-09-01     # first sync: how far back to pull\n'
          '  then in Claude Code: /hindsight-prep acme')
    return rc


def main(argv=None):
    p = argparse.ArgumentParser(prog='hindsight')
    p.add_argument('--version', action='version', version=__import__('importlib.metadata').metadata.version('hindsight'))
    sub = p.add_subparsers(dest='cmd', required=True)
    for name, fn, extra in [
        ('pending', cmd_pending, [('files', {'nargs': '*', 'help': 'saved LIST_MEETINGS results'}),
                                  ('--from-log', {'action': 'store_true'}),
                                  ('--hours', {'type': float, 'default': 3})]),
        ('normalize', cmd_normalize, [('files', {'nargs': '+', 'help': 'saved results, or meeting ids with --from-log'}),
                                      ('--from-log', {'action': 'store_true'})]),
        ('verify', cmd_verify, []),
        ('unverified', cmd_unverified, []),
        ('embed', cmd_embed, []),
        ('digest', cmd_digest, [('--new', {'action': 'store_true'})]),
        ('reconciled', cmd_reconciled, []),
        ('search', cmd_search, [('question', {}), ('-k', {'type': int, 'default': 8})]),
        ('extract', cmd_extract, [('ids', {'nargs': '*'}), ('--model', {})]),
        ('reconcile', cmd_reconcile, [('--full', {'action': 'store_true'}), ('--model', {})]),
        ('eval', cmd_eval, [('-k', {'type': int, 'default': 5}), ('--min', {'type': int, 'default': 37})]),
    ]:
        s = sub.add_parser(name, help=fn.__doc__)
        s.add_argument('client')
        for arg, kw in extra:
            s.add_argument(arg, **kw)
        s.set_defaults(fn=fn)
    p_add = sub.add_parser('add-client', help=cmd_add_client.__doc__)
    p_add.add_argument('client', help='short slug, e.g. acme')
    p_add.add_argument('--name')
    p_add.add_argument('--domain', action='append', default=[], help='attendee email domain (repeatable)')
    p_add.add_argument('--keyword', action='append', default=[], help='meeting title keyword (repeatable)')
    p_add.set_defaults(fn=cmd_add_client)
    sub.add_parser('doctor', help=cmd_doctor.__doc__).set_defaults(fn=cmd_doctor)
    p_setup = sub.add_parser('setup', help=cmd_setup.__doc__)
    p_setup.add_argument('--schedule', action='store_true', help='also install the launchd job (sync 07:00 and 19:00)')
    p_setup.set_defaults(fn=cmd_setup)
    p_sync = sub.add_parser('sync', help=cmd_sync.__doc__)
    p_sync.add_argument('clients', nargs='*')
    p_sync.add_argument('--since', help='ISO date/time; default 3 days before each client\'s newest meeting')
    p_sync.add_argument('--no-fetch', action='store_true', help='skip Granola; only process transcripts on disk')
    p_sync.set_defaults(fn=cmd_sync)
    p_serve = sub.add_parser('serve', help='run the MCP server on stdio')
    p_serve.set_defaults(fn=lambda a: __import__('hindsight.server', fromlist=['main']).main())
    a = p.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up live in log files (launchd)
    sys.exit(a.fn(a) or 0)


if __name__ == '__main__':
    main()
