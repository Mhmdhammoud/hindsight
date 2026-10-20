"""Per-client storage, quote verification, and fact search. Every function takes a client and
only reads that client's directory: HINDSIGHT_HOME/clients/<client>/."""
import hashlib
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

KINDS = ['decisions', 'commitments', 'open_questions', 'facts', 'changes']
TAG = {'decisions': 'D', 'commitments': 'C', 'open_questions': 'Q', 'facts': 'F', 'changes': 'X'}
REPO = Path(__file__).resolve().parent.parent
GRANOLA = 'https://notes.granola.ai/d/'


def home() -> Path:
    """HINDSIGHT_HOME, else <repo>/data in a checkout that already has one, else ~/.hindsight."""
    if os.environ.get('HINDSIGHT_HOME'):
        return Path(os.environ['HINDSIGHT_HOME'])
    return REPO / 'data' if (REPO / 'data').is_dir() else Path.home() / '.hindsight'


def client_dir(client: str) -> Path:
    # Validate before touching the filesystem so a client name can never escape its own directory.
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', client or ''):
        raise ValueError(f'invalid client name: {client!r}')
    d = home() / 'clients' / client
    if not (d / 'client.json').is_file():
        raise ValueError(f'unknown client: {client!r} (no {d}/client.json)')
    return d


def list_clients() -> list[dict]:
    root = home() / 'clients'
    out = []
    for d in sorted(root.glob('*/client.json')) if root.exists() else []:
        cfg = json.loads(d.read_text())
        out.append({'client': d.parent.name, 'name': cfg.get('name'),
                    'meetings': len(list((d.parent / 'extract').glob('*.json')))})
    return out


def config(client: str) -> dict:
    return json.loads((client_dir(client) / 'client.json').read_text())


# --- meetings -----------------------------------------------------------------------------

def matches(cfg: dict, title: str, emails: list[str], mid: str = '') -> str | None:
    """Why a Granola meeting belongs to this client (title keyword or attendee domain), else None.
    include_ids / exclude_ids (8-char id prefixes) override both, for meetings the rules misfile.
    Dedupe is by meeting id only: two people recording the same call produce two ids, and both
    are kept (research deduped by title+time and silently dropped 4d88cdd8)."""
    if any(mid.startswith(p) for p in cfg.get('exclude_ids', [])):
        return None
    if any(mid.startswith(p) for p in cfg.get('include_ids', [])):
        return 'id'
    for kw in cfg.get('title_keywords', []):
        if re.search(r'(?<![a-z0-9])' + re.escape(kw.lower()), (title or '').lower()):
            return f'title:{kw}'
    domains = {d.lower() for d in cfg.get('domains', [])}
    for e in emails:
        if e.lower().rsplit('@', 1)[-1] in domains:
            return f'domain:{e.lower().rsplit("@", 1)[-1]}'
    return None


def parse_meeting_list(text: str) -> list[dict]:
    """Meetings from a GRANOLA_MCP_LIST_MEETINGS result (raw tool output or its saved file)."""
    text = _unwrap(text)
    out = []
    for m in re.finditer(r'<meeting (.*?)>(.*?)</meeting>', text, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if not attrs.get('id'):
            continue
        emails = re.findall(r'[\w.+-]+@[\w.-]+\.\w+', m.group(2).replace('&lt;', '<').replace('&gt;', '>'))
        out.append({'id': attrs.get('id'), 'title': _html(attrs.get('title', '')),
                    'at': attrs.get('meeting_at') or attrs.get('created_at') or attrs.get('date'), 'emails': emails})
    return out


def _html(s):
    return s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")


def _unwrap(text: str) -> str:
    """MCP results arrive as {"data": {"data": "..."}} or [{"type": "text", "text": "..."}]; return the payload string."""
    try:
        s = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(s, list):
        s = json.loads(s[0]['text']) if s and s[0].get('text', '').lstrip().startswith('{') else {'data': {'data': s[0]['text']}}
    d = s.get('data', s)
    return d.get('data', d) if isinstance(d, dict) else d


def parse_transcript(text: str) -> dict:
    """{id, title, meeting_at, transcript} from a GRANOLA_MCP_GET_MEETING_TRANSCRIPT result."""
    payload = _unwrap(text)
    if isinstance(payload, dict):
        return payload
    return json.loads(payload[payload.index('{'):])


def save_transcript(client: str, raw_text: str) -> dict:
    """Write transcripts/<id>.txt (first line '# title | meeting_at') and keep the source in raw/.
    Returns how much it differs from a previously stored transcript."""
    d = client_dir(client)
    o = parse_transcript(raw_text)
    mid, body = o['id'], o.get('transcript') or ''
    if not body.strip():
        raise ValueError(f'{mid}: empty transcript')
    path = d / 'transcripts' / f'{mid}.txt'
    old = path.read_text().split('\n', 1)[-1] if path.exists() else None
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"# {o.get('title')} | {o.get('meeting_at')}\n{body}")
    (d / 'raw').mkdir(exist_ok=True)
    (d / 'raw' / f'{mid}.src').write_text(raw_text)
    idx_path = d / 'meetings.json'
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else []
    idx = [m for m in idx if m['id'] != mid] + [{'id': mid, 'title': o.get('title'), 'at': o.get('meeting_at'),
                                                  **{k: o[k] for k in ('link', 'event') if o.get(k)}}]
    idx_path.write_text(json.dumps(sorted(idx, key=lambda m: m['at'] or ''), indent=1))
    res = {'id': mid, 'chars': len(body), 'previous_chars': len(old) if old is not None else None}
    if old is not None:
        a, b = _norm(old).split(), _norm(body).split()
        res['word_similarity'] = round(SequenceMatcher(None, a, b, autojunk=False).ratio(), 3) if a else 0.0
    return res


def links(client: str) -> dict[str, str]:
    """{meeting id: Granola URL}. API-synced meetings store web_url; older (MCP) ids are note UUIDs."""
    p = client_dir(client) / 'meetings.json'
    return {m['id']: m['link'] for m in json.loads(p.read_text()) if m.get('link')} if p.exists() else {}


def transcript(client: str, mid: str) -> str:
    return (client_dir(client) / 'transcripts' / f'{mid}.txt').read_text()


def extracts(client: str) -> list[dict]:
    """All extracts, in date order (ties by id, matching the research facts ordering inside a day)."""
    es = [json.loads(p.read_text()) for p in sorted((client_dir(client) / 'extract').glob('*.json'))]
    return sorted(es, key=lambda e: e.get('date') or '')


def stored_ids(client: str, what: str = 'extract') -> set[str]:
    """Meeting ids that have an extract (fully ingested) or, with what='transcripts', a saved transcript."""
    return {p.stem for p in (client_dir(client) / what).glob('*.*')}


# --- duplicate recordings ---------------------------------------------------------------

def _shingles(text: str) -> set[str]:
    w = re.findall(r'[a-z0-9]+', text.split('\n', 1)[-1].lower())
    return {' '.join(w[i:i + 4]) for i in range(len(w) - 3)}


def find_duplicates(client: str, threshold: float = 0.2) -> dict[str, str]:
    """Two people recording the same call give two meeting ids, often with different titles. Pair meetings within a
    day of each other whose transcripts share >= threshold of 4-word shingles (first client: same-call pairs 0.27-0.81,
    different calls <= 0.06), or that share a calendar event id on the same day (catches a late joiner's short
    recording; same-day guard because some recurring series reuse one id). Titles are never compared. The longer
    transcript is primary. Writes and returns {duplicate_id: primary_id} (duplicates.json)."""
    d = client_dir(client)
    ms = {m['id']: m for m in json.loads((d / 'meetings.json').read_text())} if (d / 'meetings.json').exists() else {}
    tx = {p.stem: p.read_text() for p in (d / 'transcripts').glob('*.txt')}
    sh = {m: _shingles(t) for m, t in tx.items()}
    day = lambda m: (ms.get(m, {}).get('at') or tx[m].split('\n', 1)[0].rsplit('| ', 1)[-1])[:10]
    from datetime import date
    ids, parent = sorted(tx), {}
    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            try:
                near = abs((date.fromisoformat(day(a)) - date.fromisoformat(day(b))).days) <= 1
            except ValueError:
                near = False
            ev = ms.get(a, {}).get('event')
            same_event = ev and ev == ms.get(b, {}).get('event') and day(a) == day(b)
            if near and (same_event or sh[a] and sh[b] and len(sh[a] & sh[b]) / min(len(sh[a]), len(sh[b])) >= threshold):
                ra, rb = parent.get(a, a), parent.get(b, b)  # union, keeping the longest transcript as root
                keep, drop = (ra, rb) if len(tx[ra]) >= len(tx[rb]) else (rb, ra)
                parent = {k: (keep if v == drop else v) for k, v in parent.items()}
                parent[drop] = keep
    dup = {k: v for k, v in parent.items() if k != v}
    (d / 'duplicates.json').write_text(json.dumps(dup, indent=1, sort_keys=True) + '\n')
    return dup


def duplicates(client: str) -> dict[str, str]:
    p = client_dir(client) / 'duplicates.json'
    return json.loads(p.read_text()) if p.exists() else {}


# --- quote verification -------------------------------------------------------------------

def _norm(s: str) -> str:
    return ' '.join(re.sub(r"[^a-z0-9']+", ' ', s.lower().replace('’', "'")).split())


class QuoteChecker:
    """Checks quotes against one transcript: normalized exact match, else fuzzy match of a
    same-length word window around the best anchor (handles small transcription differences)."""

    def __init__(self, transcript_text: str, threshold: float = 0.85):
        self.raw = transcript_text
        self.text = _norm(transcript_text)
        self.words = self.text.split()
        # normalized word n -> (start, end) of its whitespace token in raw, so suggestions are verbatim
        self.spans = [(m.start(), m.end()) for m in re.finditer(r'\S+', transcript_text) for _ in _norm(m.group()).split()]
        self.threshold = threshold
        self.sm = SequenceMatcher(autojunk=True)
        self.sm.set_seq2(self.words)

    def check(self, quote: str) -> bool:
        # Extractors often join two fragments with '...'; every fragment must be present.
        parts = [p for p in (_norm(x) for x in re.split(r'\.\.\.|…', quote or '')) if p]
        return bool(parts) and all(self._part(p) for p in parts)

    def _part(self, q: str) -> bool:
        if f' {q} ' in f' {self.text} ':
            return True
        qw = q.split()
        if len(qw) < 3:
            return False
        self.sm.set_seq1(qw)
        blocks = sorted(self.sm.get_matching_blocks()[:-1], key=lambda b: -b.size)[:3]
        for i, j, _ in blocks:
            start = j - i
            for s in range(start - 2, start + 3):
                for n in (len(qw) - 1, len(qw), len(qw) + 1):
                    w = ' '.join(self.words[max(s, 0):max(s, 0) + n])
                    if SequenceMatcher(None, q, w, autojunk=False).ratio() >= self.threshold:
                        return True
        return False


    def closest(self, q: str) -> tuple[float, str]:
        """Best-matching transcript span for a quote (or an item's text when it has no quote): (ratio, verbatim span)."""
        qn = _norm(q)
        qw = qn.split()
        if not qw or not self.words:
            return 0.0, ''
        self.sm.set_seq1(qw)
        best = (0.0, 0, 0)
        for i, j, _ in sorted(self.sm.get_matching_blocks()[:-1], key=lambda b: -b.size)[:5]:
            for s in range(max(j - i - 3, 0), j - i + 4):
                for n in range(max(len(qw) - 3, 1), len(qw) + 4):
                    r = SequenceMatcher(None, qn, ' '.join(self.words[s:s + n]), autojunk=False).ratio()
                    best = max(best, (r, s, min(s + n, len(self.words))))
        r, s, e = best
        return round(r, 2), (self.raw[self.spans[s][0]:self.spans[e - 1][1]] if e > s else '')


def unverified(client: str) -> list[dict]:
    """Items whose quote does not verify, with the closest transcript span, so an agent can fix the quote or drop it."""
    d, out = client_dir(client), []
    for p in sorted((d / 'extract').glob('*.json')):
        e = json.loads(p.read_text())
        tpath = d / 'transcripts' / f"{e['id']}.txt"
        checker = QuoteChecker(tpath.read_text().split('\n', 1)[-1]) if tpath.exists() else None
        for k in KINDS:
            for n, x in enumerate(e.get(k, [])):
                if checker and x.get('quote') and checker.check(x['quote']):
                    continue  # checked live, so it is read-only and current mid-edit
                text = x.get('text') or f"{x.get('owner')}: {x.get('what')}"
                ratio, span = checker.closest(x.get('quote') or text) if checker else (0.0, '')
                out.append({'id': e['id'], 'kind': k, 'index': n, 'text': text, 'quote': x.get('quote'),
                            'closest': span, 'ratio': ratio})
    return out


def verify(client: str) -> dict:
    """Mark every extracted item verified: true/false in place. Items without a quote are false."""
    d = client_dir(client)
    total = ok = 0
    for p in sorted((d / 'extract').glob('*.json')):
        e = json.loads(p.read_text())
        tpath = d / 'transcripts' / f"{e['id']}.txt"
        checker = QuoteChecker(tpath.read_text().split('\n', 1)[-1]) if tpath.exists() else None
        for k in KINDS:
            for x in e.get(k, []):
                x['verified'] = bool(checker and x.get('quote') and checker.check(x['quote']))
                total += 1
                ok += x['verified']
        p.write_text(json.dumps(e, indent=2, ensure_ascii=False) + '\n')
    return {'items': total, 'verified': ok, 'unverified': total - ok}


# --- items and search ---------------------------------------------------------------------

def item_text(x: dict, title: str = '') -> str:
    """Embedded text: meeting title and topic give context the fact text alone lacks."""
    t = x.get('text') or f"{x.get('owner')}: {x.get('what')}"
    ctx = ' | '.join(c for c in (title, x.get('topic')) if c)
    return f"{ctx + ': ' if ctx else ''}{t} — \"{x.get('quote', '')}\""


def items(client: str) -> list[dict]:
    """Flat list of extracted items, primary recordings only. Order = sorted extract files x KINDS."""
    out, dup = [], duplicates(client)
    for p in sorted((client_dir(client) / 'extract').glob('*.json')):
        if p.stem in dup:
            continue
        e = json.loads(p.read_text())
        for k in KINDS:
            for n, x in enumerate(e.get(k, [])):
                text = item_text(x, e.get('title', ''))
                out.append({'key': hashlib.sha1(f"{e['id']}|{k}|{text}".encode()).hexdigest()[:16],
                            'ref': f"{e['id'][:8]}.{TAG[k]}{n}", 'id': e['id'], 'date': e.get('date'), 'title': e.get('title'), 'kind': k,
                            'text': x.get('text') or f"{x.get('owner')}: {x.get('what')}",
                            'quote': x.get('quote'), 'verified': x.get('verified', False), 'embed_text': text})
    return out


def vectors_path(client: str) -> Path:
    return client_dir(client) / 'vectors.npz'


def load_vectors(client: str) -> dict[str, np.ndarray]:
    p = vectors_path(client)
    if not p.exists():
        return {}
    z = np.load(p)
    return dict(zip(z['keys'].tolist(), z['vecs']))


def save_vectors(client: str, vecs: dict[str, np.ndarray]):
    keys = sorted(vecs)
    np.savez(vectors_path(client), keys=np.array(keys), vecs=np.array([vecs[k] for k in keys], dtype=np.float32))


def embed_new(client: str, embed) -> dict:
    """Embed items that have no vector yet; drop vectors whose item no longer exists."""
    its, vecs = items(client), load_vectors(client)
    todo = [i for i in its if i['key'] not in vecs]
    if todo:
        for i, v in zip(todo, embed([i['embed_text'] for i in todo], False)):
            vecs[i['key']] = v
    live = {i['key'] for i in its}
    save_vectors(client, {k: v for k, v in vecs.items() if k in live})
    return {'items': len(its), 'embedded': len(todo)}


tok = lambda s: re.findall(r'[a-z0-9]+', s.lower())


def exact_terms(q: str) -> list[str]:
    """Tokens that look like names/IDs, where keyword match beats meaning: quoted phrases,
    tokens with digits, or words with inner capitals (e.g. "4d88cdd8", "HotSOS", "\"Text2Mall\"")."""
    quoted = re.findall(r'"([^"]+)"', q)
    rest = [w for w in re.findall(r'[\w-]+', re.sub(r'"[^"]+"', ' ', q))
            if re.search(r'\d', w) or re.search(r'[a-z][A-Z]|[A-Z]{2,}', w)]
    return quoted + rest


def search(client: str, question: str, k: int = 8, embed=None) -> list[dict]:
    """Gemini cosine ranking over extracted items. BM25 only appends exact name/ID hits the
    embedding ranking missed (fusing them hurt ranking in research), or replaces it if no embedder."""
    k = max(1, min(int(k), 50))
    its, vecs = items(client), load_vectors(client)
    ranked: list[int] = []
    if embed is not None:
        idx = [n for n, i in enumerate(its) if i['key'] in vecs]
        if idx:
            D = np.array([vecs[its[n]['key']] for n in idx])
            qv = embed([question], True)[0]
            ranked = [idx[n] for n in np.argsort(-(D @ qv))[:k]]
    terms = exact_terms(question) if ranked else [question]
    extra = []
    if terms and its:
        from rank_bm25 import BM25Okapi
        bm = BM25Okapi([tok(i['embed_text']) for i in its])
        scores = bm.get_scores(tok(' '.join(terms)))
        for n in np.argsort(-scores):
            if scores[n] <= 0 or len(extra) >= (3 if ranked else k):
                break
            hay = _norm(its[n]['embed_text'])
            if n not in ranked and (not ranked or any(_norm(t) in hay for t in terms)):
                extra.append(int(n))
    out, sup, lk = [], superseded(client), links(client)
    for n, how in [(n, 'semantic') for n in ranked] + [(n, 'keyword') for n in extra]:
        i = its[n]
        out.append({'text': i['text'], 'quote': i['quote'], 'verified': i['verified'], 'kind': i['kind'],
                    'date': i['date'], 'meeting_title': i['title'], 'meeting_id': i['id'],
                    'link': lk.get(i['id'], GRANOLA + i['id']), 'match': how,
                    **({'superseded_by': sup[i['ref']]} if i['ref'] in sup else {})})
    return sorted(out, key=lambda r: 'superseded_by' in r)  # current facts first, order kept otherwise


def superseded(client: str) -> dict[str, str]:
    """{item ref: what replaced it}, written by the reconcile step (superseded.json). Refs are
    <meeting id8>.<kind letter><index>, as shown in the digest."""
    # ponytail: refs are list positions, so editing an extract after reconcile can misattribute; re-reconcile then
    p = client_dir(client) / 'superseded.json'
    return json.loads(p.read_text()) if p.exists() else {}


# --- reconciled documents -----------------------------------------------------------------

def reconciled(client: str) -> set[str]:
    """Meeting ids already folded into brief/commitments/changes (reconciled.json)."""
    p = client_dir(client) / 'reconciled.json'
    return set(json.loads(p.read_text())) if p.exists() else set()


def mark_reconciled(client: str, ids: set[str] | None = None, drop: str | None = None) -> int:
    """Record meetings as folded into the docs: `ids` (default: every stored meeting) are added; `drop` is removed
    (a re-extracted meeting must be reconciled again)."""
    done = reconciled(client) | (stored_ids(client) | set(duplicates(client)) if ids is None else ids)
    done.discard(drop)
    (client_dir(client) / 'reconciled.json').write_text(json.dumps(sorted(done), indent=1) + '\n')
    return len(done)


def digest(client: str, new_only: bool = False) -> str:
    """Extracts in date order, one compact block per meeting: the input to the reconcile step. Duplicate
    recordings fold into their primary's header; new_only keeps meetings not yet reconciled."""
    dup, done, out = duplicates(client), reconciled(client), []
    also = {}
    for d, p in dup.items():
        also.setdefault(p, []).append(d[:8])
    for e in extracts(client):
        group = {e['id'], *(d for d, p in dup.items() if p == e['id'])}
        if e['id'] in dup or (new_only and group <= done):
            continue
        ids = ', '.join(f'id:{i}' for i in [e['id'][:8], *also.get(e['id'], [])])
        out.append(f"## {e.get('date')} | {e.get('title')} | {'INTERNAL' if e.get('internal') else 'CLIENT'} | {ids}")
        out.append(e.get('summary', ''))
        for k in KINDS:
            for n, x in enumerate(e.get(k, [])):
                flag = '' if x.get('verified') else ' [unverified]'
                t = f'{TAG[k]}{n}'  # with the header's first id this is the item's ref, e.g. d040909a.D0
                if k == 'commitments':
                    out.append(f"- {t}: [{x.get('owner')}→{x.get('to')}] {x.get('what')} (due {x.get('due')}){flag}")
                elif k == 'facts':
                    out.append(f"- {t}/{x.get('topic')}: {x.get('text')}{flag}")
                else:
                    owner = f" (owner: {x['owner']})" if k == 'open_questions' and x.get('owner') else ''
                    out.append(f"- {t}: {x.get('text')}{owner}{flag}")
        out.append('')
    return '\n'.join(out)


def read_doc(client: str, name: str) -> str:
    p = client_dir(client) / name
    return p.read_text() if p.exists() else f'({name} not built yet; run hindsight-sync {client})'


def open_commitments(client: str, owner: str | None = None) -> str:
    """Rows of the '## Open' table in commitments.md, optionally filtered by owner substring."""
    text = read_doc(client, 'commitments.md')
    m = re.search(r'^## Open.*?$(.*?)(?=^## |\Z)', text, re.S | re.M)
    if not m:
        return text
    lines = m.group(1).strip().splitlines()
    head = [l for l in lines[:2] if l.startswith('|')]
    rows = [l for l in lines if l.startswith('|') and l not in head]
    if owner:
        rows = [r for r in rows if owner.lower() in r.split('|')[1].lower()]
    return '\n'.join(head + rows) if rows else f'No open commitments{" for " + owner if owner else ""}.'


DATE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def what_changed(client: str, since: str | None = None) -> str:
    """changes.md bullets whose cited date is >= since (all when since is None), plus raw changes and
    decisions extracted from meetings on/after since. Unverified quotes are flagged."""
    doc = read_doc(client, 'changes.md')
    if not since:
        return doc
    keep = []
    for line in doc.splitlines():
        dates = DATE.findall(line)
        if line.startswith('#') or (dates and max(dates) >= since):
            keep.append(line)
    raw = []
    for e in extracts(client):
        if (e.get('date') or '') < since:
            continue
        for k in ('changes', 'decisions'):
            for x in e.get(k, []):
                flag = '' if x.get('verified') else ' [unverified quote]'
                raw.append(f"- {k[:-1]}: {x.get('text')} ({e['date']}, {e['id'][:8]}){flag}")
    return '\n'.join(keep) + (f'\n\n## Raw extracted changes and decisions since {since}\n' + '\n'.join(raw) if raw else '')


def meeting(client: str, mid: str) -> dict:
    hits = [p for p in (client_dir(client) / 'extract').glob('*.json') if p.stem.startswith(mid)] if len(mid) >= 6 else []
    if len(hits) != 1:
        raise ValueError(f'{"no" if not hits else "ambiguous"} meeting {mid!r} for {client}')
    e = json.loads(hits[0].read_text())
    return {**e, 'link': links(client).get(e['id'], GRANOLA + e['id'])}
