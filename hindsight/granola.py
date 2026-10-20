"""Granola public API (https://docs.granola.ai): list notes and fetch transcripts directly, so sync needs no
Claude Code session. Key: GRANOLA_API_KEY, else macOS Keychain item `hindsight-granola`."""
import functools
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import requests

API = 'https://public-api.granola.ai/v1'


@functools.cache
def _key() -> str:
    k = os.environ.get('GRANOLA_API_KEY')
    if not k:
        r = subprocess.run(['security', 'find-generic-password', '-s', 'hindsight-granola', '-w'],
                           capture_output=True, text=True)
        k = r.stdout.strip()
    if not k:
        raise RuntimeError('no Granola API key: run `security add-generic-password -s hindsight-granola -a granola -w`')
    return k


def _get(path: str, **params) -> dict:
    for attempt in range(6):
        r = requests.get(f'{API}{path}', params=params, headers={'Authorization': f'Bearer {_key()}'}, timeout=60)
        if r.status_code in (429, 500, 502, 503):  # 5 req/s sustained
            time.sleep(min(2 ** attempt, 30))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def list_notes(created_after: str) -> list[dict]:
    """Note summaries (id, title, owner, created_at; no attendees) created on/after an ISO date/time, minus deleted ones."""
    if 'T' in created_after:  # the API rejects '+00:00' offsets: send UTC with Z
        created_after = datetime.fromisoformat(created_after.replace('Z', '+00:00')).astimezone(timezone.utc) \
            .strftime('%Y-%m-%dT%H:%M:%SZ')
    out, cursor = [], None
    while True:
        page = _get('/notes', created_after=created_after, page_size=30, **({'cursor': cursor} if cursor else {}))
        out += [n for n in page['notes'] if not n.get('deleted_at')]
        cursor = page.get('cursor')
        if not page.get('hasMore') or not cursor:
            return out


def note(note_id: str) -> dict:
    """Full note with transcript; large transcripts come from the paginated transcript endpoint."""
    try:
        return _get(f'/notes/{note_id}', include='transcript')
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 413:
            raise
    n, lines, cursor = _get(f'/notes/{note_id}'), [], None
    while True:
        page = _get(f'/notes/{note_id}/transcript', page_size=100, **({'cursor': cursor} if cursor else {}))
        lines += page['transcript']
        cursor = page.get('cursor')
        if not page.get('hasMore') or not cursor:
            return {**n, 'transcript': lines}


def emails(n: dict) -> list[str]:
    ev = n.get('calendar_event') or {}
    return [e for e in [*(a.get('email') for a in n.get('attendees') or []),
                        *(i.get('email') for i in ev.get('invitees') or []), ev.get('organiser')] if e]


def start(n: dict) -> str:
    return (n.get('calendar_event') or {}).get('scheduled_start_time') or n.get('created_at') or ''


def stored_id(n: dict) -> str:
    """The id meetings are stored under: the note UUID in web_url (same id the Granola MCP uses, so API and older
    MCP-synced meetings line up), else the API id without its `not_` prefix."""
    m = re.search(r'/d/([0-9a-f-]{36})', n.get('web_url') or '')
    return m.group(1) if m else n['id'].removeprefix('not_')


def to_transcript(n: dict) -> dict:
    """{id, title, meeting_at, transcript, link} in the same text layout as the Granola MCP transcripts."""
    def who(s):
        if s.get('source') == 'microphone':
            return 'Microphone'
        return f"System audio ({s['name']})" if s.get('name') else 'System audio'
    body = '\n\n'.join(f"{who(t.get('speaker') or {})}: {t.get('text', '').strip()}" for t in n.get('transcript') or [])
    return {'id': stored_id(n), 'title': n.get('title') or '', 'meeting_at': start(n), 'transcript': body,
            'link': n.get('web_url'), 'event': (n.get('calendar_event') or {}).get('calendar_event_id')}
