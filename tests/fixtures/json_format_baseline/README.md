# ?format=json baselines (v0.1.7 c9)

Pre-amendment capture (commit 5384d2c, before the speech_id/cite_url
amendment): every file here is the EXACT ?format=json body the pre-amendment
serializer emitted, with the live token redacted to the suite's TEST_TOKEN
(`hg_testvalidtoken0123456789abcdef`).

* `report_bill-773.json` / `report_bill-774.json` / `report_bill-775.json` /
  `report_017_19931012_S0003_T0003.json` — LIVE reports fetched from the local
  container (127.0.0.1:8766, v0.1.6) 2026-09-21. NOT rendered by the offline
  suite; consumed by `tests/test_json_citation.py` as the value-identity
  comparison baseline (the serializer is pure over the same models, so the
  offline-rendered bill-774-equivalent content is not required for the
  existing-field identity proof — the offline E2E report below is).
* `report_037_20041019_S0004_T0023.json` — the offline E2E report (respx
  stubbed `topic_20041019_saf.json`), captured in-process with the same
  TestClient the suite uses.

c9 (approved amendment) deliberately re-pins these ONCE: the only delta is
the two appended per-speech keys `speech_id` + `cite_url` (and nothing else).
`tests/test_json_citation.py` asserts the delta is EXACTLY those two fields.
