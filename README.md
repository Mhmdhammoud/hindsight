# Hindsight

Per-client meeting memory. Turns a pile of meeting transcripts into a living brief:
what was decided, what changed, who owes what, and a search that finds answers by meaning.

Each person runs it on their own machine against their own Granola meetings; client data
never leaves `~/.hindsight`.

## Install (macOS, ~5 minutes)

Needs: [uv](https://docs.astral.sh/uv), [Claude Code](https://claude.com/claude-code) logged in (`claude /login`),
and two keys:
- Granola API key: Granola app > Settings > Connectors > API keys (Personal + Public scopes; Business plan).
- Gemini API key: [AI Studio](https://aistudio.google.com/apikey). Optional; without it search is keyword-only.

Pick the latest tag from [Releases](https://github.com/Mhmdhammoud/hindsight/releases):

    uv tool install git+https://github.com/Mhmdhammoud/hindsight@v0.2.0
    hindsight setup               # asks for both keys (stored in Keychain), links skills, registers the MCP server
    hindsight setup --schedule    # optional: also sync every client at 07:00 and 19:00 (launchd)

`hindsight setup` is safe to re-run; `hindsight doctor` should say ok on every line. Upgrade: the same
`uv tool install --force ...@<new tag>`, then `hindsight setup` again.
Over SSH the Keychain is locked: put `GRANOLA_API_KEY=...` and `GEMINI_API_KEY=...` in `~/.hindsight/.env`
(`chmod 600`) instead. `~/.hindsight/team.json` (written by setup) lists your company and people so extraction can tell internal from client meetings.

Add a client and pull its meetings:

    hindsight add-client acme --name "Acme Corp" --domain acme.com --keyword Acme
    hindsight sync acme --since 2026-09-01

Then in Claude Code: `/hindsight-prep acme`, or ask anything; the `hindsight` MCP tools (client_brief,
open_commitments, what_changed, search, meeting) answer from your data.

What a sync does: Granola API → transcripts → Claude (`claude -p`, Opus) extracts decisions, commitments, open
questions, facts and changes with verbatim quotes → quotes checked against the transcript (failures get repaired
or dropped) → Gemini embeddings for search → Claude reconciles brief.md, commitments.md, changes.md.
Costs: runs on your Claude Code plan; embeddings bill to the Gemini API key's project.

## Why

One client alone had ~85 transcribed meetings in 3 months, and the team kept forgetting things.
Transcripts exist (Granola), but nothing tracks state over time.

## What the research showed (2026-10-14, first client, 61 unique meetings)

**Extraction + reconciliation is the core value.** Per-meeting extraction → reconciled brief
surfaced things people forgot:
- a decision reversal made a week earlier (verified in transcript)
- a commitment open for 68 days
- overdue client-owed inputs
- an unconfirmed payment

**Search: Gemini embeddings over extracted facts wins.** 40 questions, answer in top-5 (MRR):

| method | raw chunks | extracted facts |
|---|---|---|
| BM25 keyword | 25/40 (0.47) | 29/40 (0.59) |
| bge-base (local) | 21/40 (0.36) | 34/40 (0.74) |
| OpenAI text-embedding-3-large | 24/40 (0.50) | 37/40 (0.79) |
| **Gemini gemini-embedding-2** | 27/40 (0.55) | **38/40 (0.89)** |
| Gemini + BM25 (RRF) | 28/40 (0.53) | 39/40 (0.78) |

Takeaways:
- Extracting facts first matters more than the embedding model (+10–16 answers for every provider).
- Gemini ranks the right answer #1 most often. Hybrid with BM25 adds recall but hurts ranking.
- Embeddings can't answer "what's true *now*": they return superseded facts. Only the reconciled brief can.
- LLM-extracted "quotes" were only ~63% verbatim. Quotes must be verified against the transcript.
- Granola speech-to-text mangles names ("HotSOS" → "hot sauce"). Embeddings help; keyword search doesn't.

**Existing tools don't replace it:**
- Granola `query_granola_meetings` answers one-off questions well. But it caps how many meetings
  it reads and got a reversed decision backwards.
- Goldfish `get_promises` catches email/screen promises the meeting pipeline can't see.
  But it's personal and has no meeting decisions.
- Hindsight should *complement* both, not rebuild them.


## Data

Client data lives in `~/.hindsight/clients/<client>/` (in a repo checkout with a `data/` folder: there instead). Each client is fully isolated.

## Commands

    hindsight sync [client ...]           # Granola API -> extract -> verify -> embed -> reconcile (all clients by default)
    hindsight sync acme --no-fetch        # only transcripts already on disk
    hindsight reconcile acme --full       # rebuild brief/commitments/changes from every meeting
    hindsight extract acme <id>           # re-extract one meeting
    hindsight search acme "who owns the payroll export?"
    hindsight doctor                      # check claude, Gemini key, Granola key, team.json
    hindsight serve                       # MCP server (stdio); `hindsight setup` registers it with Claude Code

Other agents or bots that speak MCP (Claude Desktop, Cursor, Codex, ...) can use the same server; point them at
`hindsight serve` over stdio, e.g.

    {"mcpServers": {"hindsight": {"command": "/Users/<you>/.local/bin/hindsight", "args": ["serve"]}}}

Config lives in `~/.hindsight/.env` (see `.env.example`): `HINDSIGHT_MODEL`
(default claude-opus-5-5), `HINDSIGHT_CLAUDE` (the Claude CLI or a wrapper such as `claude-proxy`; default `claude`), `HINDSIGHT_HOME`. The Granola and Gemini keys live in Keychain (items `hindsight-granola`, `hindsight-gemini`), or
`GRANOLA_API_KEY` / `GEMINI_API_KEY` in `.env`.
A client folder with `client.json` renamed to `client.json.parked` is skipped.

Tests: `uv run pytest` (no network; the LLM and embedder are faked).

Develop from a checkout: `uv tool install --editable . --force && hindsight setup`.
Release: bump `version` in pyproject.toml, then `git tag vX.Y.Z && git push origin vX.Y.Z`; the Release workflow
runs the tests and publishes a GitHub release with the wheel.
