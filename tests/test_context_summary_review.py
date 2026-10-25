import json

from eventide.context_summary import parse_structured_summary
from tests.test_context_summary import valid_summary


def test_quotes_match_decoded_message_body_including_newlines():
    summary = valid_summary()
    for key in ("hard_constraints", "facts", "checks", "failures"):
        summary[key][0]["src"] = [{"msg": 0, "quote": 'do not\nrun "publish"'}]
    parsed, reason = parse_structured_summary(
        json.dumps(summary), [{"role": "user", "content": 'Please do not\nrun "publish" now.'}]
    )
    assert reason is None
    assert parsed is not None


def test_message_metadata_is_not_source_text():
    summary = valid_summary()
    for key in ("hard_constraints", "facts", "checks", "failures"):
        summary[key][0]["src"] = [{"msg": 0, "quote": "content"}]
    parsed, reason = parse_structured_summary(
        json.dumps(summary), [{"role": "user", "content": "actual words"}]
    )
    assert parsed is None
    assert reason
