from pathlib import Path


def test_custom_gpt_can_complete_a_specific_calibration_task() -> None:
    instructions = (
        Path(__file__).resolve().parents[1] / "CUSTOM_GPT_INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")

    assert "完成最新校准任务" in instructions
    assert "完成校准任务 <advisory_id>" in instructions
    assert "不要求用户重复说明背景" in instructions
    assert "用户指定 advisory ID 时必须选择该待办" in instructions
