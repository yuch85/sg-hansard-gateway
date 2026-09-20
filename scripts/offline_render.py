"""Offline rendering + live fetching for the baseline capture tool (Phase 27.3).

The two ways a corpus entry's HTML/text is obtained, factored out of
``machine_baseline.py`` so each file stays under the 300-LOC STYLE.md cap:

* :func:`offline_client` — an in-process TestClient over the committed test
  index (the SAME fixture index as ``tests/conftest.py``) so the offline
  surface matches what the regression test renders. The public base URL is the
  DEFAULT ``settings.public_base_url`` (the render layer reads the module-level
  singleton, not a per-app override); the offline regression test renders with
  the same default, so hrefs match deterministically.
* :func:`render_offline` — render one offline entry (respx-stubbing the
  upstream for entries whose route hits SPRS, e.g. the date TOC).
* :func:`live_get` — GET one path on the live deployment with the real token.

Kept import-light (FastAPI/respx imported lazily) so the capture script can be
imported for its pure functions without a full app environment.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from baseline_corpus import CorpusEntry, entry_path, offline_index_rows, text_path

#: The fixed offline token (must match hansard_gateway.auth.TEST_TOKEN).
OFFLINE_TOKEN: str = "hg_testvalidtoken0123456789abcdef"

#: The live public host (report + search entries are fetched here).
LIVE_BASE_URL: str = "https://hansard.098020.xyz"

#: The SPRS upstream base + the PAIR base (respx stub targets for the date TOC).
UPSTREAM_BASE: str = "https://sprs.parl.gov.sg/search"
PAIR_BASE: str = "https://search.pair.gov.sg"

#: The committed searchResult fixture (the deterministic stub for the date sweep).
_SEARCH_FIXTURE: Path = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "searchresult_20041019_p1.json"
)


def offline_client() -> Any:
    """Build an in-process TestClient over the committed test index.

    Uses the SAME fixture index as ``tests/conftest.py`` so the offline surface
    matches what the regression test renders.
    """
    import tempfile as _tf

    import hansard_gateway.auth as auth_mod
    from fastapi.testclient import TestClient
    from hansard_gateway.auth import TEST_TOKEN, TEST_TOKEN_LABEL, TokenStore
    from hansard_gateway.index.build import build_index
    from hansard_gateway.index.loader import IndexService
    from hansard_gateway.main import create_app

    tmp = _tf.mkdtemp(prefix="hgb_")
    db = Path(tmp) / "index.db"
    build_index(offline_index_rows(), db)
    index = IndexService(path=db)

    tok_path = Path(tmp) / "tokens.yaml"
    tok_path.write_text(
        "tokens:\n"
        f"- label: {TEST_TOKEN_LABEL}\n"
        f"  sha256: {hashlib.sha256(TEST_TOKEN.encode('utf-8')).hexdigest()}\n"
        "  enabled: true\n"
        '  last4: "cdef"\n',
        encoding="utf-8",
    )
    auth_mod._store = TokenStore(path=tok_path)
    return TestClient(create_app(index_override=index))


def render_offline(client: Any, entry: CorpusEntry) -> tuple[str, bytes]:
    """Render one offline entry; return (html, text_bytes).

    For ``upstream_stub`` entries (the date TOC) the route hits the SPRS
    upstream (a sitting sweep) — respx-stub it with the committed searchResult
    fixture so the capture is deterministic and reproducible (the same stub the
    offline regression test uses).
    """
    route = f"/a/{OFFLINE_TOKEN}{entry_path(entry)}"
    text_route = f"/a/{OFFLINE_TOKEN}{text_path(entry)}"
    if entry.upstream_stub:
        html, text_bytes = _render_stubbed_upstream(client, route, text_route)
    else:
        html = client.get(route).text
        text_bytes = client.get(text_route).content
    if not html:
        raise SystemExit(f"render_offline: {entry.name} -> empty body")
    return html, text_bytes


def _render_stubbed_upstream(client: Any, route: str,
                             text_route: str) -> tuple[str, bytes]:
    """Render an upstream-dependent offline entry under a respx stub."""
    import respx

    rows = json.loads(_SEARCH_FIXTURE.read_text(encoding="utf-8"))
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            html = client.get(route).text
            text_bytes = client.get(text_route).content
        finally:
            pair.stop()
    return html, text_bytes


def live_get(path: str, *, token: str) -> str:
    """GET ``path`` on the live deployment with the token; return the body."""
    import urllib.request

    url = f"{LIVE_BASE_URL}/a/{token}{path}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")
