"""Load ~/.hindsight/.env and, in a repo checkout, <repo>/.env (KEY=value lines) so the CLI, MCP server and
launchd job share one config. Real environment variables win."""
import os
from pathlib import Path

for _env in (Path.home() / '.hindsight' / '.env', Path(__file__).resolve().parent.parent / '.env'):
    if _env.is_file():
        for line in _env.read_text().splitlines():
            k, sep, v = line.strip().partition('=')
            if sep and k and not k.startswith('#'):
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))
