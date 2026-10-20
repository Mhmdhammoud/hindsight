"""Headless pipeline: quote repair/drop, reconcile ref checking, Granola note mapping (fake LLM, no network)."""
import json

from hindsight import core, granola, pipeline
from test_core import TRANSCRIPT, home, make_client  # noqa: F401 (fixture)

EMPTY = {k: [] for k in ['decisions', 'commitments', 'open_questions', 'facts', 'people', 'changes']}


def test_extract_repairs_then_drops(home, monkeypatch):
    make_client(home, 'acme', {'id': 'm1aaaaaa', 'title': 'Kickoff', 'date': '2026-09-22', **EMPTY})
    calls = []

    def fake(prompt, schema, model=None, timeout=0):
        calls.append(prompt)
        if len(calls) == 1:
            return {**EMPTY, 'internal': False, 'summary': 's', 'decisions': [
                {'text': 'Use Data Bridge', 'quote': "We're going to proceed with Infor HMS Data Bridge"},
                {'text': 'Reports twice a day', 'quote': 'reports arrive two times daily by mail'},
                {'text': 'Invented', 'quote': 'we will rebuild HotSOS next quarter'}]}
        return {'fixes': [{'n': 0, 'quote': 'The hot sauce reports come in by email twice a day'}, {'n': 1, 'quote': ''}]}
    monkeypatch.setattr(pipeline, 'ask', fake)
    r = pipeline.extract('acme', 'm1aaaaaa', repairs=1)
    e = json.loads((home / 'clients/acme/extract/m1aaaaaa.json').read_text())
    assert r == {'id': 'm1aaaaaa', 'items': 2, 'repaired': 1, 'dropped': 1}
    assert [x['text'] for x in e['decisions']] == ['Use Data Bridge', 'Reports twice a day']
    assert all(x['verified'] for x in e['decisions']) and e['date'] == '2026-09-22' and e['title'] == 'Kickoff'
    assert len(calls) == 2 and 'reports arrive two times daily' in calls[1]  # repair saw only the failing quotes
    assert 'proceed with Infor' not in calls[1].split('<transcript>')[0]


def test_reconcile_drops_unknown_refs(home, monkeypatch):
    make_client(home, 'acme', {'id': 'm1aaaaaa', 'title': 'K', 'date': '2026-09-22', **EMPTY,
                               'decisions': [{'text': 'A', 'quote': 'q', 'verified': True}]})
    monkeypatch.setattr(pipeline, 'ask', lambda *a, **k: {
        'brief': '# B', 'commitments': '# C', 'changes': '# X',
        'superseded': [{'ref': 'm1aaaaaa.D0', 'by': 'later'}, {'ref': 'zzzzzzzz.D9', 'by': 'nope'}]})
    r = pipeline.reconcile('acme')
    assert r['mode'] == 'full' and r['dropped_unknown_refs'] == ['zzzzzzzz.D9'] and r['reconciled'] == 1
    assert core.superseded('acme') == {'m1aaaaaa.D0': 'later'}
    assert core.read_doc('acme', 'brief.md') == '# B\n'
    assert pipeline.reconcile('acme') == {'client': 'acme', 'skipped': 'nothing new'}


def test_granola_note_mapping():
    n = {'id': 'not_1d3tmYTlCICgjy', 'title': 'ACME Sync', 'web_url': 'https://notes.granola.ai/d/631df7f6-c509-4811-badc-2dc64fcdf74e',
         'created_at': '2026-10-14T16:31:00Z', 'attendees': [{'name': 'P', 'email': 'p@acme-hotels.com'}],
         'calendar_event': {'scheduled_start_time': '2026-10-14T16:30:00Z', 'invitees': [{'email': 'a@b.com'}],
                            'organiser': 'lead@vendor.example'},
         'transcript': [{'speaker': {'source': 'microphone'}, 'text': 'Hi '},
                        {'speaker': {'source': 'speaker', 'name': 'Pat'}, 'text': 'Hello'}]}
    t = granola.to_transcript(n)
    assert t['id'] == '631df7f6-c509-4811-badc-2dc64fcdf74e' and t['meeting_at'] == '2026-10-14T16:30:00Z' and t['link'] == n['web_url']
    assert t['transcript'] == 'Microphone: Hi\n\nSystem audio (Pat): Hello'
    assert set(granola.emails(n)) == {'p@acme-hotels.com', 'a@b.com', 'lead@vendor.example'}
    assert granola.stored_id({**n, 'web_url': None}) == '1d3tmYTlCICgjy'


def test_meeting_date():
    assert pipeline.meeting_date('2026-08-05T00:00:00.000+03:00', 'America/New_York') == '2026-08-04'


def test_shared_meeting_dedup_ignores_titles(home, monkeypatch):
    """Three attendees record one call under different titles: only the longest is extracted. A late joiner's short
    recording with little text overlap still folds in through the shared calendar event id."""
    call = ' '.join(f'word{n} about the payroll schedule' for n in range(60))
    d = make_client(home, 'acme', {'id': 'm1aaaaaa', 'title': 'x', 'date': '2026-10-19', **EMPTY})
    (d / 'extract' / 'm1aaaaaa.json').unlink()
    tx = {'m1aaaaaa': f'# Acme | Weekly Review | 2026-10-19T19:00:00Z\n{call} and more words at the end',
          'm2bbbbbb': f'# Weekly sync w/ Acme | 2026-10-19T19:01:00Z\n{call}',
          'm3cccccc': '# Acme call | 2026-10-19T19:40:00Z\ncompletely different late joiner chatter here today'}
    for mid, t in tx.items():
        (d / 'transcripts' / f'{mid}.txt').write_text(t)
    (d / 'meetings.json').write_text(json.dumps([
        {'id': m, 'title': t.split(' | ')[0][2:], 'at': t.split(' | ')[-1].split('\n')[0], 'event': 'ev_20261019T190000Z'}
        for m, t in tx.items()]))
    done = []
    monkeypatch.setattr(pipeline, 'extract', lambda c, m, *a, **k: done.append(m) or {'id': m, 'items': 0})
    monkeypatch.setattr(pipeline, 'reconcile', lambda c: {})
    monkeypatch.setattr(core, 'embed_new', lambda c, e: {})
    pipeline.sync('acme')
    assert done == ['m1aaaaaa']
    assert core.duplicates('acme') == {'m2bbbbbb': 'm1aaaaaa', 'm3cccccc': 'm1aaaaaa'}


def test_reextract_unreconciles(home, monkeypatch):
    make_client(home, 'acme', {'id': 'm1aaaaaa', 'title': 'Kickoff', 'date': '2026-09-22', **EMPTY})
    core.mark_reconciled('acme')
    monkeypatch.setattr(pipeline, 'ask', lambda *a, **k: {**EMPTY, 'internal': False, 'summary': 's'})
    pipeline.extract('acme', 'm1aaaaaa')
    assert 'm1aaaaaa' not in core.reconciled('acme')


def test_fetch_checkpoint_only_advances_without_errors(home, monkeypatch):
    make_client(home, 'acme', {'id': 'm1aaaaaa', 'title': 'K', 'date': '2026-09-22', **EMPTY})
    (home / 'clients/acme/client.json').write_text(json.dumps({'name': 'Acme', 'domains': ['acme.com'], 'title_keywords': []}))
    notes = {'not_aaaaaaaaaaaaaa': {'id': 'not_aaaaaaaaaaaaaa', 'title': 'Weekly', 'web_url': None,
                                     'attendees': [{'email': 'x@acme.com'}], 'created_at': '2026-10-19T19:00:00Z',
                                     'transcript': [{'speaker': {'source': 'speaker'}, 'text': 'hello there'}]},
             'not_bbbbbbbbbbbbbb': None}  # metadata request fails

    def get(path, **kw):
        n = notes[path.rsplit('/', 1)[-1]]
        if n is None:
            raise RuntimeError('HTTP 500')
        return n
    monkeypatch.setattr(granola, 'list_notes', lambda after: [{'id': k} for k in notes])
    monkeypatch.setattr(granola, '_get', get)
    monkeypatch.setattr(granola, 'note', lambda nid: notes[nid])
    out = pipeline.fetch(['acme'])
    assert [x['id'] for x in out['acme']] == ['aaaaaaaaaaaaaa', 'not_bbbbbbbbbbbbbb'] and 'error' in out['acme'][1]
    assert not (home / 'clients/acme/sync.json').exists()  # an error: the next run re-covers this window
    notes['not_bbbbbbbbbbbbbb'] = {**notes['not_aaaaaaaaaaaaaa'], 'id': 'not_bbbbbbbbbbbbbb', 'attendees': []}
    out = pipeline.fetch(['acme'])
    assert out['acme'] == [] and (home / 'clients/acme/sync.json').exists()  # stored note skipped, other unmatched


def test_list_notes_sends_utc_z(monkeypatch):
    seen = []
    monkeypatch.setattr(granola, '_get', lambda path, **kw: seen.append(kw['created_after']) or
                        {'notes': [{'id': 'not_x', 'deleted_at': None}, {'id': 'not_y', 'deleted_at': '2026-01-01'}],
                         'hasMore': False, 'cursor': None})
    assert [n['id'] for n in granola.list_notes('2026-10-17T23:00:00+03:00')] == ['not_x']
    assert granola.list_notes('2026-10-17') and seen == ['2026-10-17T20:00:00Z', '2026-10-17']
