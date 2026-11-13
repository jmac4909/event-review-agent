from copy import deepcopy
import json

from event_review.context import model_context


def test_compact_context_preserves_dates_conditions_and_evidence_without_changing_audit():
    source = {"occurred_at": "2026-11-12T00:57:10Z", "region_id": "europe",
              "site": "North warehouse", "zone": "Aisle 2", "camera": "EU-CAM-07",
              "metadata": {"people_count": 1, "zone_marker": "restricted"}}
    correction = {"id": "correction-1", "event_snapshot": source, "decision": "rejected",
                  "severity": None, "reason": "No restrictions today on entering zones",
                  "created_at": "2026-11-12T00:58:00Z", "match_scores": {"combined": 0.9}}
    guidance = {"id": "guidance-1", "source_correction_id": "correction-1", "event_snapshot": source,
                "decision": "rejected", "severity": None, "metadata_condition": source["metadata"],
                "text": "EU-CAM-07 · 12 Nov 2026, 00:57 UTC no restrictions today on entering zones",
                "decided_at": "2026-11-12T00:58:36Z", "decided_by": "supervisor-europe",
                "match_scores": {"combined": 0.95}}
    audit = {"instructions": "Review only supplied evidence", "event": source | {"occurred_at": "2026-11-14T00:57:10Z"},
             "site_rules": {"effective_from": "2026-01-01", "version": 1},
             "memory": {"corrections": [correction], "approved_guidance": [guidance],
                        "guidance_overflow": False, "guidance_conflict": False},
             "memory_selection": {"method": "hybrid", "candidates": ["correction-1", "guidance-1"]},
             "lookups_remaining": 2, "tool_results": [{"request": {"name": "get_site_context"}, "result": {"layout": "No barrier recorded"}}]}
    before = deepcopy(audit)
    prompt = model_context(audit)
    assert audit == before
    assert prompt["event"]["occurred_at"].startswith("2026-11-14")
    assert prompt["memory"]["approved_guidance"][0] == {key: value for key, value in guidance.items() if key != "match_scores"}
    assert prompt["memory"]["corrections"][0] == {key: value for key, value in correction.items() if key != "match_scores"}
    assert prompt["site_rules"] == audit["site_rules"]
    assert prompt["tool_results"] == audit["tool_results"] and prompt["lookups_remaining"] == 2
    assert "memory_selection" not in prompt
    assert len(json.dumps(prompt)) < len(json.dumps(audit))
