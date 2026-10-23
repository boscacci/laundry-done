"""Exercise the actual deployed flow graph: only a human keypress unlocks audio."""

import json
from pathlib import Path
import pytest

FLOW = (
    Path(__file__).resolve().parents[2] / "infra/phone-alerts/outbound-flow.json.tftpl"
)


def execute_flow(digit=None, input_error=None):
    flow = json.loads(FLOW.read_text())
    actions = {a["Identifier"]: a for a in flow["Actions"]}
    current = flow["StartAction"]
    audio, callbacks = [], []
    for _ in range(12):
        a = actions[current]
        p, transitions = a["Parameters"], a["Transitions"]
        if a["Type"] == "DisconnectParticipant":
            return audio, callbacks
        if a["Type"] == "GetParticipantInput":
            # Even an instructional prompt can become a voicemail message.
            assert not ({"Text", "PromptId", "Media"} & p.keys())
            assert p.get("SSML") == '<speak><break time="100ms"/></speak>'
            assert 1 <= int(p["InputTimeLimitSeconds"]) <= 10
            condition = next(
                (
                    c
                    for c in transitions.get("Conditions", [])
                    if c["Condition"] == {"Operator": "Equals", "Operands": [digit]}
                ),
                None,
            )
            if digit is not None and not input_error and condition:
                current = condition["NextAction"]
            else:
                error = input_error or (
                    "InputTimeLimitExceeded" if digit is None else "NoMatchingCondition"
                )
                current = next(
                    e["NextAction"]
                    for e in transitions["Errors"]
                    if e["ErrorType"] == error
                )
            continue
        if a["Type"] == "MessageParticipant":
            audio.append(p)
        elif a["Type"] == "InvokeLambdaFunction":
            callbacks.append(p["LambdaInvocationAttributes"]["outcome"])
        else:
            pytest.fail("Unexpected flow action: " + a["Type"])
        current = transitions["NextAction"]
    pytest.fail("Flow did not disconnect")


@pytest.mark.parametrize("digit", [None, "0", "2", "*", "#"])
def test_no_keypress_or_wrong_key_never_plays_voicemail(digit):
    audio, callbacks = execute_flow(digit)
    assert audio == []
    assert callbacks == ["no_answer"]


@pytest.mark.parametrize(
    "error", ["InputTimeLimitExceeded", "NoMatchingError", "NoMatchingCondition"]
)
def test_input_errors_fail_closed_without_audio(error):
    assert execute_flow("1", error) == ([], ["no_answer"])


def test_press_one_plays_selected_recording_then_records_completion():
    audio, callbacks = execute_flow("1")
    assert len(audio) == 1
    assert audio[0]["Media"]["Uri"] == "$.Attributes.audio_uri"
    assert callbacks == ["completed"]
