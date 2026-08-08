from __future__ import annotations

from pathlib import Path

import yaml


def test_fixed_calibration_suites_have_required_case_counts() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "calibration-suites.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert document["schema_version"] == 1
    assert document["scoring"] == {
        "total_points": 100,
        "pass_score": 80,
        "require_zero_hard_errors": True,
        "final_decision": "user",
    }
    assert len(document["suites"]["creator"]["cases"]) == 3
    assert len(document["suites"]["researcher"]["cases"]) == 5
    assert len(document["suites"]["critic"]["cases"]) == 5

    for agent_id, suite in document["suites"].items():
        assert suite["suite_version"].startswith(f"{agent_id}-baseline-v")
        assert len({case["case_id"] for case in suite["cases"]}) == len(suite["cases"])
