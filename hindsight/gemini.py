"""Gemini gemini-embedding-2 via the Gemini API. No task_type: the model takes task prefixes in the text.
Key: GEMINI_API_KEY, else macOS Keychain item `hindsight-gemini` (an AI Studio key; no gcloud needed)."""
import functools
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent'


@functools.cache
def _key() -> str:
    k = os.environ.get('GEMINI_API_KEY')
    if not k:
        r = subprocess.run(['security', 'find-generic-password', '-s', 'hindsight-gemini', '-w'],
                           capture_output=True, text=True)
        k = r.stdout.strip()
    if not k:  # callers fall back to keyword search on RuntimeError
        raise RuntimeError('no Gemini API key: run `security add-generic-password -s hindsight-gemini -a gemini -w`')
    return k


def _one(text: str, query: bool) -> list[float]:
    body = {'content': {'parts': [{'text': (
        f'task: question answering | query: {text}' if query else f'title: none | text: {text}')}]}}
    for attempt in range(8):
        r = requests.post(URL, headers={'x-goog-api-key': _key()}, json=body, timeout=120)
        if r.status_code in (429, 500, 503):
            time.sleep(min(2 ** attempt, 30))
            continue
        r.raise_for_status()
        return r.json()['embedding']['values']
    raise RuntimeError(f'embedding failed after retries: HTTP {r.status_code} {r.text[:200]}')


def embed(texts: list[str], query: bool) -> np.ndarray:
    """L2-normalized vectors, one request per text on a small thread pool."""
    with ThreadPoolExecutor(8) as ex:
        v = np.array(list(ex.map(lambda t: _one(t, query), texts)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)
