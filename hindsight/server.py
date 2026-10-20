"""Hindsight MCP server (stdio). Every tool is scoped to one client; nothing reads across clients."""
import functools
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import core

mcp = MCPServer('hindsight', instructions=(
    'Per-client meeting memory built from Granola transcripts. client_brief is the reconciled current state '
    '(use it for "what is true now"); search finds individual extracted facts. A result with superseded_by '
    'was replaced by a later meeting; superseded results come last, and older ones may still be untagged. '
    'Items marked verified=false have a quote that could not be found in the transcript; treat them with care.'))


def tool(fn):
    """Register fn as a tool; bad input (unknown client, bad id) comes back as a readable tool error."""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, LookupError) as e:
            raise ToolError(str(e)) from e
    return mcp.tool()(wrapped)


@tool
def list_clients() -> str:
    """Clients with a Hindsight store, and how many meetings each has."""
    return json.dumps(core.list_clients(), indent=1)


@tool
def client_brief(client: str) -> str:
    """The reconciled living brief for a client: current state, workstreams, decisions, risks. Every bullet cites (date, meeting id)."""
    return core.read_doc(client, 'brief.md')


@tool
def open_commitments(client: str, owner: str | None = None) -> str:
    """Open commitments table (oldest first). owner filters by a substring of the Owner column, e.g. "Mo"."""
    return core.open_commitments(client, owner)


@tool
def what_changed(client: str, since: str | None = None) -> str:
    """Reversals and updates to earlier plans. since=YYYY-MM-DD limits to changes cited on/after that date and adds the raw changes/decisions extracted since then."""
    return core.what_changed(client, since)


@tool
def search(client: str, question: str, k: int = 8) -> str:
    """Semantic search over extracted facts, decisions, commitments, questions and changes. Returns text, quote,
    verified flag, date, meeting title/id and a Granola link. Superseded items carry superseded_by and sort last; check client_brief for current state."""
    from . import gemini
    try:
        res = core.search(client, question, k, gemini.embed)
    except RuntimeError as e:  # Gemini unavailable: keyword search still answers
        res = core.search(client, question, k, None)
        res.insert(0, {'warning': f'semantic search unavailable ({e}); keyword results only'})
    return json.dumps(res, indent=1, ensure_ascii=False)


@tool
def meeting(client: str, id: str) -> str:
    """One meeting's summary and full extract (decisions, commitments, questions, facts, people, changes). id may be the first 8+ chars."""
    return json.dumps(core.meeting(client, id), indent=1, ensure_ascii=False)


def main():
    mcp.run()
