"""Independent source annotations remain frozen and use the unchanged fact grader."""

import copy
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from eventide.eval_suite import BenchmarkTask, grade_facts

FIXTURE = Path(__file__).parent / "fixtures/quality/independent-v1.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
TASKS = DATA["tasks"]

# Reviewed physical source lines supporting each annotation, in fact order.
SOURCE_LINES = {
    "constellation-caption-call": [
        "    return frame_text(TITLE, body)", 'TITLE = "北斗七星"',
    ],
    "kiln-cycle-states": [
        '    return {"phase": "drying", "remaining": duration}',
        '        cycle["phase"] = "finished"',
    ],
    "radio-sample-packet": [
        "    return sum(samples) // len(samples)",
        '    return level.to_bytes(2, byteorder="big")',
    ],
    "matrix-transpose-expressions": [
        "        return []", "    return list(map(list, zip(*rows)))",
    ],
    "datagram-valid-rejected": [
        "    if checksum == expected:", '        raise ValueError("checksum mismatch")',
    ],
    "deferred-lines-closed-source": [
        '        return (line.rstrip("\\n") for line in source)',
        '    with path.open(encoding="utf-8") as source:',
    ],
    "rectangular-pixel-stride": [
        "    return offset_pixel(pixels, height, width, row, col)",
        "    return pixels[row * width + col]",
    ],
    "decoder-final-state-overwrite": [
        '        job["state"] = "failed"', '        job["state"] = "ready"',
    ],
}

# These independent assertions verify the task premises against executable fixture code.
BEHAVIOR = {
    "constellation-caption-call": (
        "from poster import render_poster\n"
        'assert render_poster("星座").splitlines()[1] == "北斗七星"\n'
    ),
    "kiln-cycle-states": (
        "from controller import one_step\n"
        "from plant.cycle import start\n"
        'assert start(2)["phase"] == "drying"\n'
        'assert one_step() == {"phase": "finished", "remaining": 0}\n'
    ),
    "radio-sample-packet": (
        "from transmit import build_packet\n"
        "from radio.signal import fold_samples\n"
        "assert fold_samples([9, 10]) == 9\n"
        'assert build_packet([2, 6]) == bytes([0, 4])\n'
    ),
    "matrix-transpose-expressions": (
        "from axes import rotate_axes\n"
        "assert rotate_axes([]) == []\n"
        "assert rotate_axes([[1, 2], [3, 4]]) == [[1, 3], [2, 4]]\n"
    ),
    "datagram-valid-rejected": (
        "from receive import receive\n"
        "from wire.datagram import checksum8\n"
        "assert checksum8(bytes([3, 8])) == 11\n"
        "try:\n"
        "    receive(bytes([3, 8]), 11)\n"
        "except ValueError as error:\n"
        '    assert str(error) == "checksum mismatch"\n'
        "else:\n"
        '    raise AssertionError("valid checksum was not rejected")\n'
    ),
    "deferred-lines-closed-source": (
        "from pathlib import Path\n"
        "from preview import first_line\n"
        "try:\n"
        '    first_line(Path("sample.txt"))\n'
        "except ValueError as error:\n"
        '    assert "closed file" in str(error)\n'
        "else:\n"
        '    raise AssertionError("closed-source failure was not reproduced")\n'
    ),
    "rectangular-pixel-stride": (
        "from screen import read_pixel\n"
        "from pixels.access import offset_pixel\n"
        "assert offset_pixel(list(range(6)), 3, 2, 1, 1) == 4\n"
        "assert read_pixel(list(range(6)), 3, 2, 1, 1) == 3\n"
    ),
    "decoder-final-state-overwrite": (
        "from ingest import decode_job\n"
        "def fail(source):\n"
        '    raise ValueError("invalid waveform")\n'
        'job = {"source": "sample.wav"}\n'
        'assert decode_job(job, fail) == "ready"\n'
        'assert "wave" not in job\n'
    ),
}


def test_independent_quality_inputs_and_annotations_are_frozen():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "f263225b16a5891a900ac97b77106d8bc6e16144a1e9e6179f5149c3eb798d16"
    )
    assert DATA["schema_version"] == DATA["version"] == 1
    assert DATA["base_commit"] == "7099f92"
    assert len(TASKS) == len({task["id"] for task in TASKS}) == 8
    assert Counter(task["category"] for task in TASKS) == {"understanding": 4, "diagnosis": 4}
    assert all(task["split"] == "holdout" for task in TASKS)
    assert set(SOURCE_LINES) == set(BEHAVIOR) == {task["id"] for task in TASKS}
    inputs = [
        {key: task[key] for key in ("id", "version", "category", "prompt", "workspace")}
        for task in TASKS
    ]
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == DATA["input_sha256"] == (
        "328a5bd450acd5b00abd4ef0e451e86af06914eaa2a0afa4ba50098383104e54"
    )
    assert any("\r\n" in source for task in TASKS for source in task["workspace"].values())
    assert any("北斗七星" in source for task in TASKS for source in task["workspace"].values())


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task["id"])
def test_independent_annotations_and_unchanged_strict_grader(task):
    validated = BenchmarkTask.model_validate(task)
    assert len(validated.grade.facts) == 2
    for fact, source_line in zip(task["grade"]["facts"], SOURCE_LINES[task["id"]], strict=True):
        assert fact["start_line"] == fact["end_line"]
        assert task["workspace"][fact["path"]].splitlines()[fact["start_line"] - 1] == source_line
        assert fact["id"] in task["prompt"]
    gold = json.dumps({"facts": task["grade"]["facts"]}, ensure_ascii=False)
    assert grade_facts(task, gold) == ()
    assert grade_facts(task, f"```json\n{gold}\n```")
    assert grade_facts(task, task["script"][-1]["text"]) == ()
    for field, replacement in (("id", "wrong-id"), ("answer", "wrong-answer"), ("start_line", 99)):
        wrong = {"facts": copy.deepcopy(task["grade"]["facts"])}
        wrong["facts"][0][field] = replacement
        assert grade_facts(task, json.dumps(wrong))


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task["id"])
def test_independent_task_premises_execute_offline(tmp_path, task):
    for name, source in task["workspace"].items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.encode("utf-8"))
        if target.suffix == ".py":
            compile(target.read_bytes(), name, "exec")
    result = subprocess.run(
        [sys.executable, "-c", BEHAVIOR[task["id"]]], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
