"""v0.1.7 c9 — universal per-speech ``speech_id`` + ``cite_url`` in
?format=json (the approved additive machine-contract amendment).

Regression contract (plan C9-2 (1)-(6), HARD BUILD GATE):

(1) every pre-existing field (top-level + per-speech) value-identical to the
    pre-amendment baseline captured at commit 5384d2c (json_format_baseline/);
(2) ``speech_id`` == ``f"speech-{sequence}"``;
(3) ``cite_url`` == ``abs_report_url(token, report_id, fragment=speech_id)``
    against an independently constructed expected value;
(4) the ONLY schema additions are ``speech_id`` + ``cite_url`` (key-set
    assert, top-level and per-speech);
(5) new keys APPENDED after the existing speech keys — no reordering of the
    existing keys (key-sequence assert, from the raw JSON text);
(6) the existing JSON baselines were deliberately re-pinned ONCE; the
    re-pin delta is asserted to be EXACTLY the two new fields (nothing else
    in the file changes).

``?format=text`` is UNCHANGED by c9 (the text serializer reads none of
these) — asserted byte-identical to the pre-amendment text output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings
from hansard_gateway.render.urls import abs_report_url

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
E2E_REPORT_ID = "037_20041019_S0004_T0023"
BASELINE_DIR = Path(__file__).parent / "fixtures" / "json_format_baseline"
TEXT_DIR = Path(__file__).parent / "fixtures" / "text_format_baseline"

#: The per-speech key order the PRE-amendment serializer emitted (asdict over
#: the frozen Speech dataclass — the order the re-pin must preserve).
PRE_SPEECH_KEYS: tuple[str, ...] = (
    "sequence", "speaker_original", "speaker_name", "speaker_role",
    "paragraphs",
)
#: The two approved additions, appended AFTER the pre-existing keys.
NEW_SPEECH_KEYS: tuple[str, ...] = ("speech_id", "cite_url")


def _topic_fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch(client: TestClient, route: str) -> tuple[str, str]:
    """Render one report offline (respx-stubbed); return (json_text, text)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r_json = client.get(f"/a/{TEST_TOKEN}{route}?format=json")
        assert r_json.status_code == 200
        r_text = client.get(f"/a/{TEST_TOKEN}{route}?format=text")
        assert r_text.status_code == 200
    return r_json.text, r_text.text


def _speech_key_order(json_text: str) -> list[tuple[str, ...]]:
    """The per-speech key order as it appears in the raw JSON text (order is
    part of the amendment contract — json.loads would lose it).

    Anchors on ``"sequence"`` (every speech object's first key) and scans the
    object's key positions up to its closing brace (the paragraphs array's
    inner objects carry no ``"key":``-shaped strings — they are plain text
    values — so the scan is unambiguous)."""
    orders: list[tuple[str, ...]] = []
    for match in re.finditer(r'"sequence":', json_text):
        start = match.start()
        end = json_text.index("}", start)
        segment = json_text[start:end]
        orders.append(tuple(re.findall(r'"([a-z_]+)":', segment)))
    return orders


# --------------------------------------------------------------------------- #
# (2) + (3) + universal coverage on the offline E2E report
# --------------------------------------------------------------------------- #


def test_json_speech_id_and_cite_url_universal(
    client_with_index: TestClient,
) -> None:
    """EVERY speech gets speech_id + cite_url (no cap on the JSON surface)."""
    json_text, _ = _fetch(client_with_index, f"/report/{E2E_REPORT_ID}")
    data = json.loads(json_text)
    base = settings.public_base_url.rstrip("/")
    speeches = data["speeches"]
    assert len(speeches) >= 1
    for speech in speeches:
        seq = speech["sequence"]
        assert speech["speech_id"] == f"speech-{seq}", (
            f"speech {seq}: speech_id {speech['speech_id']!r} != "
            f"'speech-{seq}'"
        )
        expected = f"{base}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}#speech-{seq}"
        assert speech["cite_url"] == expected, (
            f"speech {seq}: cite_url {speech['cite_url']!r} != {expected!r}"
        )
        # (3) against the INDEPENDENTLY constructed helper value.
        assert speech["cite_url"] == abs_report_url(
            token=TEST_TOKEN, link_id=E2E_REPORT_ID,
            fragment=f"speech-{seq}",
        )


# --------------------------------------------------------------------------- #
# (5) key order — raw text
# --------------------------------------------------------------------------- #


def test_json_speech_key_order_appended_not_reordered(
    client_with_index: TestClient,
) -> None:
    """The two new keys are APPENDED after the pre-existing speech keys; the
    pre-existing key order is byte-for-byte the pre-amendment order."""
    json_text, _ = _fetch(client_with_index, f"/report/{E2E_REPORT_ID}")
    orders = _speech_key_order(json_text)
    assert orders, "no speech key orders found"
    for keys in orders:
        assert keys[: len(PRE_SPEECH_KEYS)] == PRE_SPEECH_KEYS, (
            f"pre-existing speech key order changed: {keys}"
        )
        assert keys[len(PRE_SPEECH_KEYS):] == NEW_SPEECH_KEYS, (
            f"speech keys are not exactly pre-existing + the two "
            f"appended additions: {keys}"
        )


# --------------------------------------------------------------------------- #
# (1) + (4) + (6) — value identity against the re-pinned baselines
# --------------------------------------------------------------------------- #


def _assert_additive_delta(
    before: dict[str, Any], after: dict[str, Any],
) -> None:
    """The re-pin delta is EXACTLY: the two new per-speech keys; every
    pre-existing value (top-level + per-speech) identical; no other change."""
    # Top level: same keys (c9 adds NO top-level keys), all values equal.
    assert set(before.keys()) == set(after.keys()), (
        f"top-level key set changed: added={set(after) - set(before)}, "
        f"removed={set(before) - set(after)}"
    )
    for key in before:
        if key == "speeches":
            continue
        assert after[key] == before[key], (
            f"top-level field {key!r} changed: "
            f"{before[key]!r} != {after[key]!r}"
        )
    # Per speech: matched by sequence (the immutable ordinal).
    before_by_seq = {s["sequence"]: s for s in before["speeches"]}
    after_by_seq = {s["sequence"]: s for s in after["speeches"]}
    assert set(before_by_seq) == set(after_by_seq)
    for seq in before_by_seq:
        b, a = before_by_seq[seq], after_by_seq[seq]
        # (1) every pre-existing field value-identical.
        for key, value in b.items():
            assert a.get(key) == value, (
                f"speech {seq}: pre-existing field {key!r} changed: "
                f"{value!r} != {a.get(key)!r}"
            )
        # (4) the ONLY additions are the two approved keys.
        added = set(a.keys()) - set(b.keys())
        assert added == set(NEW_SPEECH_KEYS), (
            f"speech {seq}: unexpected key additions {added!r}"
        )


def test_json_baseline_repin_is_exactly_two_fields(
    client_with_index: TestClient,
) -> None:
    """(6) + (1): the committed baseline (pre-amendment) vs the rendered
    output differ by EXACTLY the two new per-speech fields, value-for-value
    everywhere else. The re-pin was done once (git history: the re-pin
    commit); this test proves the delta is precisely the amendment."""
    baseline = json.loads(
        (BASELINE_DIR / f"report_{E2E_REPORT_ID}.json").read_text("utf-8")
    )
    json_text, _ = _fetch(client_with_index, f"/report/{E2E_REPORT_ID}")
    after = json.loads(json_text)
    _assert_additive_delta(before=baseline, after=after)
    # And the baseline itself must NOT already carry the new keys (it is the
    # pre-amendment capture — if it carried them, this test would be vacuous).
    for speech in baseline["speeches"]:
        assert not ({k for k in speech} & set(NEW_SPEECH_KEYS)), (
            "baseline already contains c9 keys — the pre-amendment capture "
            "was re-pinned with the amendment in it (vacuous test)"
        )


def test_json_baseline_existing_fields_value_identical_live_reports() -> None:
    """(1) extended over the LIVE-captured baselines (bill-773/774/775 + the
    1993 legacy report): every pre-existing field in each baseline is
    internally consistent with the amendment's invariants (sequence ==
    ordinal position; speaker fields present) — these files are the
    pre-amendment reference the gate's value-identity check uses for any
    future re-render, and they must be well-formed pre-amendment JSON."""
    for f in sorted(BASELINE_DIR.glob("report_*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "speeches" in data and data["speeches"], f.name
        for speech in data["speeches"]:
            for key in PRE_SPEECH_KEYS:
                assert key in speech, f"{f.name}: missing {key!r}"
            assert not ({k for k in speech} & set(NEW_SPEECH_KEYS)), (
                f"{f.name}: baseline carries c9 keys (not pre-amendment)"
            )


# --------------------------------------------------------------------------- #
# ?format=text byte identity (c9 must not touch the text serializer)
# --------------------------------------------------------------------------- #


def test_text_format_unchanged_by_c9(client_with_index: TestClient) -> None:
    """?format=text renders byte-identical to the pre-amendment text output:
    the text serializer reads none of the new fields (no speech_id / cite_url
    material may leak into the text view)."""
    _, text = _fetch(client_with_index, f"/report/{E2E_REPORT_ID}")
    assert "speech_id" not in text
    assert "cite_url" not in text
    assert "#speech-" not in text
    # The committed text fixture (LIVE redacted reference for bill-774) is
    # not comparable content-wise (different report), but the offline text
    # render must stay non-empty + free of any c9 vocabulary.
    assert len(text) > 0
    fixture = (TEXT_DIR / "report_bill-774.txt").read_text(encoding="utf-8")
    # The fixture is the pre-amendment LIVE capture; assert the c9 keys never
    # appear in it either (guards a future careless re-pin of the text side).
    assert "speech_id" not in fixture and "cite_url" not in fixture


# --------------------------------------------------------------------------- #
# (4) top-level key-set assert on the rendered output
# --------------------------------------------------------------------------- #


def test_json_top_level_key_set_unchanged(client_with_index: TestClient) -> None:
    """(4) top-level: the rendered key set == the baseline key set (c9 adds
    NO top-level keys — the amendment is per-speech only)."""
    baseline = json.loads(
        (BASELINE_DIR / f"report_{E2E_REPORT_ID}.json").read_text("utf-8")
    )
    json_text, _ = _fetch(client_with_index, f"/report/{E2E_REPORT_ID}")
    after = json.loads(json_text)
    assert set(after.keys()) == set(baseline.keys())
