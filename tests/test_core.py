"""Quote verification, client isolation, and the search ranking path (fake embedder, no network)."""
import json

import numpy as np
import pytest

from hindsight import core

TRANSCRIPT = ("# Kickoff | 2026-09-22T15:30:00Z\nMicrophone: Okay. We're going to proceed with Infor HMS Data Bridge, "
              "not ION. System audio (Pat): The hot sauce reports come in by email twice a day. "
              "Mo: I'll send the access list by Friday.")


def make_client(home, name, extract, transcript=TRANSCRIPT):
    d = home / 'clients' / name
    (d / 'extract').mkdir(parents=True)
    (d / 'transcripts').mkdir()
    (d / 'client.json').write_text(json.dumps({'name': name, 'domains': [], 'title_keywords': []}))
    (d / 'transcripts' / f"{extract['id']}.txt").write_text(transcript)
    (d / 'extract' / f"{extract['id']}.json").write_text(json.dumps(extract))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HINDSIGHT_HOME', str(tmp_path))
    return tmp_path


def test_quote_checker():
    c = core.QuoteChecker(TRANSCRIPT)
    assert c.check("we're going to proceed with INFOR HMS data bridge")        # case + punctuation
    assert c.check('The hotsauce reports come in by email twice a day')         # small transcription diff
    assert c.check("I'll send the access list ... by Friday")                   # stitched fragments, each present
    assert not c.check('We will build our own HotSOS replacement next quarter')  # invented
    assert not c.check("I'll send the access list ... by Monday")               # one fragment invented
    assert not c.check('')


def test_closest_span_is_verbatim():
    c = core.QuoteChecker(TRANSCRIPT.split('\n', 1)[1])
    r, span = c.closest('we will go ahead with Infor HMS Data Bridge not ION')
    assert 'Infor HMS Data Bridge' in span and span in TRANSCRIPT and r > 0.6
    assert c.check(span)  # the suggestion itself verifies


def test_verify_marks_items(home):
    make_client(home, 'acme', {'id': 'm1', 'date': '2026-09-22', 'title': 'Kickoff',
                               'decisions': [{'text': 'Data Bridge', 'quote': 'proceed with Infor HMS Data Bridge'}],
                               'facts': [{'text': 'made up', 'quote': 'we signed the contract yesterday'}],
                               'open_questions': [{'text': 'no quote'}]})
    assert core.verify('acme') == {'items': 3, 'verified': 1, 'unverified': 2}
    e = json.loads((home / 'clients/acme/extract/m1.json').read_text())
    assert e['decisions'][0]['verified'] is True and e['facts'][0]['verified'] is False
    assert [(u['kind'], u['index']) for u in core.unverified('acme')] == [('open_questions', 0), ('facts', 0)]


def test_client_isolation(home):
    make_client(home, 'acme', {'id': 'a1', 'date': '2026-09-01', 'title': 'Acme', 'facts': [{'text': 'acme secret', 'quote': 'x'}]})
    make_client(home, 'globex', {'id': 'g1', 'date': '2026-09-02', 'title': 'Globex', 'facts': [{'text': 'globex secret', 'quote': 'y'}]})
    assert [c['client'] for c in core.list_clients()] == ['acme', 'globex']
    assert {i['id'] for i in core.items('acme')} == {'a1'}
    assert all('globex' not in r['text'] for r in core.search('acme', 'secret', embed=None))
    for bad in ['../globex', 'acme/../globex', '', 'ACME', 'nope']:
        with pytest.raises(ValueError):
            core.client_dir(bad)
    with pytest.raises(ValueError):
        core.meeting('acme', 'g1')  # another client's meeting id is not visible


def test_search_ranking_with_fake_embedder(home):
    make_client(home, 'acme', {'id': 'm1aaaaaa', 'date': '2026-09-22', 'title': 'Kickoff', 'facts': [
        {'topic': 'systems', 'text': 'Infor integration uses Data Bridge', 'quote': 'proceed with Infor HMS Data Bridge', 'verified': True},
        {'topic': 'data', 'text': 'Reports arrive by email twice a day', 'quote': 'come in by email twice a day', 'verified': True},
        {'topic': 'people', 'text': 'Mo owns the access list REQ-4411', 'quote': 'send the access list', 'verified': False}]})
    vocab = ['infor', 'email', 'access']

    def fake_embed(texts, query):  # bag-of-words over a tiny vocab, normalized
        v = np.array([[t.lower().count(w) + 0.01 for w in vocab] for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    assert core.embed_new('acme', fake_embed) == {'items': 3, 'embedded': 3}
    assert core.embed_new('acme', fake_embed)['embedded'] == 0  # only new items get embedded
    res = core.search('acme', 'which infor integration?', k=2, embed=fake_embed)
    assert [r['text'] for r in res][:1] == ['Infor integration uses Data Bridge']
    assert res[0]['link'] == 'https://notes.granola.ai/d/m1aaaaaa' and res[0]['verified'] is True
    assert len([r for r in res if r['match'] == 'semantic']) == 2
    # exact ID the embedding ranking missed: appended by BM25 as a keyword match, ranking untouched
    res = core.search('acme', 'email REQ-4411', k=1, embed=fake_embed)
    assert res[0]['text'].startswith('Reports arrive') and res[0]['match'] == 'semantic'
    assert any(r['match'] == 'keyword' and 'REQ-4411' in r['text'] for r in res)
    assert res[-1]['verified'] is False


def test_meeting_filter_matches_title_or_domain():
    cfg = {'domains': ['acmecorp.com'], 'title_keywords': ['ACM']}
    assert core.matches(cfg, 'ACM360 | Weekly Sync', [])
    assert core.matches(cfg, 'Quick catch up', ['jane.doe@AcmeCorp.com'])
    assert not core.matches(cfg, 'XACM review', ['a@example.com'])
    cfg.update(include_ids=['11112222'], exclude_ids=['33334444'])
    assert core.matches(cfg, 'Legal consents', ['a@example.com'], '11112222-aaaa') == 'id'
    assert not core.matches(cfg, 'ACM sync', ['b@acmecorp.com'], '33334444-bbbb')


def test_duplicate_recordings_fold_into_primary(home):
    call = ' '.join(f'word{n} about the payroll schedule' for n in range(60))
    make_client(home, 'acme', {'id': 'long1', 'date': '2026-09-22', 'title': 'Sync', 'facts': [{'text': 'a', 'quote': 'q'}]},
                transcript=f'# Sync | 2026-09-22T15:00:00Z\n{call} and some more talk at the end')
    d = home / 'clients/acme'
    (d / 'transcripts/short2.txt').write_text(f'# Sync | 2026-09-22T18:00:00Z\n{call}')
    (d / 'extract/short2.json').write_text(json.dumps({'id': 'short2', 'date': '2026-09-22', 'title': 'Sync', 'facts': [{'text': 'b', 'quote': 'q'}]}))
    (d / 'transcripts/other3.txt').write_text('# Pricing | 2026-09-22T19:00:00Z\nan entirely different conversation about pricing sheets')
    (d / 'extract/other3.json').write_text(json.dumps({'id': 'other3', 'date': '2026-09-22', 'title': 'Pricing'}))
    assert core.find_duplicates('acme') == {'short2': 'long1'}
    assert {i['id'] for i in core.items('acme')} == {'long1'}
    dg = core.digest('acme')
    assert 'id:long1, id:short2' in dg and '## 2026-09-22 | Sync' in dg and dg.count('| Sync |') == 1
    core.mark_reconciled('acme')
    assert core.digest('acme', new_only=True) == ''


def test_superseded_items_tagged_and_sorted_last(home):
    d = make_client(home, 'acme', {'id': 'old11111', 'date': '2026-10-06', 'title': 'Sync', 'decisions': [
        {'text': 'No write-back to payroll', 'quote': 'q'}, {'text': 'Write-back owner is Pat', 'quote': 'q'}],
        'facts': [{'text': f'unrelated fact {n}', 'quote': 'q'} for n in range(4)]})  # so BM25 idf is positive
    (d / 'superseded.json').write_text(json.dumps({'old11111.D0': '2026-10-13, id:new22222: write-back approved'}))
    assert '- D0: No write-back to payroll' in core.digest('acme')  # the ref the reconcile step sees
    res = core.search('acme', 'write-back', k=5, embed=None)
    assert [r['text'] for r in res] == ['Write-back owner is Pat', 'No write-back to payroll']
    assert res[1]['superseded_by'].startswith('2026-10-13') and 'superseded_by' not in res[0]
