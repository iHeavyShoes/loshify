"""The host LLM adapter across provider modes: json_schema, json_mode, plain text (fake facades)."""

import json
from types import SimpleNamespace

import pytest

from core import llm_json, writer
from core.llm_json import JsonAsker, extract_json

PORTRAIT = {"portrait": "Right now you keep coming back to Lib Artist 0, with a soft spot for slow songs."}


class FakeFacade:
    """``PluginLlm``-shaped. *supports* is the set of modes the 'provider' accepts."""

    def __init__(self, supports, answer=PORTRAIT, text=None, error=None):
        self.supports, self.answer, self.text, self.error = set(supports), answer, text, error
        self.calls = []

    def complete_structured(self, **kw):
        mode = "schema" if kw.get("json_schema") is not None else "json" if kw.get("json_mode") else "none"
        self.calls.append((mode, kw))
        if self.error:
            raise self.error
        if mode == "schema" and "schema" not in self.supports:
            raise ValueError("response_format json_schema is not supported")
        if mode == "json" and "json" not in self.supports:
            raise ValueError("Only json_schema structured output is supported")  # the real directsdk error
        text = self.text if self.text is not None else json.dumps(self.answer)
        return SimpleNamespace(text=text, parsed=extract_json(text) if self.text is None else None)

    def complete(self, messages, **kw):
        self.calls.append(("text", {"messages": messages, **kw}))
        if self.error:
            raise self.error
        if "text" not in self.supports:
            raise RuntimeError("down")
        body = self.text if self.text is not None else json.dumps(self.answer)
        return SimpleNamespace(text=f"Sure! Here it is:\n```json\n{body}\n```\nEnjoy.")


def asker(facade):
    return JsonAsker(facade, lambda t: SimpleNamespace(text=t), timeout=5, max_tokens=100)


def test_schema_mode_sends_the_purpose_schema_and_never_json_mode():
    fake = FakeFacade({"schema"})
    ask = asker(fake)
    assert ask("portrait", "write it", {"lead": []}, writer.SCHEMAS["portrait"]) == PORTRAIT
    assert [m for m, _ in fake.calls] == ["schema"]
    kw = fake.calls[0][1]
    assert kw["json_schema"] == writer.SCHEMAS["portrait"] and "json_mode" not in kw
    assert kw["schema_name"] == "loshify_portrait" and kw["purpose"] == "loshify.portrait"
    assert kw["timeout"] == 5 and kw["max_tokens"] == 100
    assert ask.last == {"mode": "schema", "tried": []}


def test_json_mode_when_the_provider_rejects_schemas_and_the_mode_is_remembered():
    fake = FakeFacade({"json"})
    ask = asker(fake)
    assert ask("portrait", "write it", {}, writer.SCHEMAS["portrait"]) == PORTRAIT
    assert [m for m, _ in fake.calls] == ["schema", "json"]
    assert ask.last["mode"] == "json" and ask.last["tried"][0][0] == "schema"
    fake.calls.clear()
    assert ask("calibration", "pick", {}, writer.SCHEMAS["calibration"]) == PORTRAIT
    assert [m for m, _ in fake.calls] == ["json"]  # straight to the mode that worked


def test_plain_text_with_tolerant_extraction_when_neither_structured_mode_works():
    fake = FakeFacade({"text"})
    ask = asker(fake)
    assert ask("portrait", "write it", {"x": 1}, writer.SCHEMAS["portrait"]) == PORTRAIT
    assert [m for m, _ in fake.calls] == ["schema", "json", "text"]
    messages = fake.calls[-1][1]["messages"]
    assert messages[0]["role"] == "system" and '"portrait"' in messages[0]["content"]  # the schema rides along
    assert json.loads(messages[1]["content"]) == {"x": 1}
    assert fake.calls[-1][1]["purpose"] == "loshify.portrait"


def test_a_purpose_without_schema_skips_schema_mode():
    fake = FakeFacade({"schema", "json"})
    assert asker(fake)("other", "x", {}, None) == PORTRAIT
    assert [m for m, _ in fake.calls] == ["json"]


def test_a_timeout_does_not_cascade_through_the_modes():
    fake = FakeFacade({"schema", "json", "text"}, error=TimeoutError("60s"))
    ask = asker(fake)
    assert ask("portrait", "x", {}, writer.SCHEMAS["portrait"]) is None
    assert len(fake.calls) == 1


def test_everything_rejected_is_none_not_an_exception():
    fake = FakeFacade(set())
    ask = asker(fake)
    assert ask("portrait", "x", {}, writer.SCHEMAS["portrait"]) is None
    assert [m for m, _ in ask.last["tried"]] == ["schema", "json", "text"]


def test_unparsed_structured_text_is_still_read_and_garbage_is_none():
    fake = FakeFacade({"schema"}, text='noise {"portrait": "from text"} trailing')
    assert asker(fake)("portrait", "x", {}, writer.SCHEMAS["portrait"]) == {"portrait": "from text"}
    fake = FakeFacade({"schema"}, text="I cannot help with that.")
    ask = asker(fake)
    assert ask("portrait", "x", {}, writer.SCHEMAS["portrait"]) is None
    assert ask.mode == "schema"  # the provider took the mode; only this answer was unusable


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": [1, {"b": "}"}]}\n```', {"a": [1, {"b": "}"}]}),
    ('Here: {"a": 1} and {"b": 2}', {"a": 1}),
    ('[1, 2] then {"a": {"n": 1}}', {"a": {"n": 1}}),
    ('{broken then {"ok": true}', {"ok": True}),
    ("no json", None), ("", None), (None, None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_is_timeout_by_name():
    class ReadTimeout(Exception):
        pass
    assert llm_json.is_timeout(ReadTimeout()) and llm_json.is_timeout(TimeoutError())
    assert not llm_json.is_timeout(ValueError("Only json_schema structured output is supported"))


def test_schemas_cover_every_purpose_and_are_strict():
    assert set(writer.SCHEMAS) == {"portrait", "calibration", "tune_in", "rotation", "rotation_spec"}

    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])

    for schema in writer.SCHEMAS.values():
        walk(schema)


def test_writer_reads_the_schema_shape_of_rotation_candidates():
    spec = {"slots": [{"name": "Fresh", "intent": "new", "mode": "discover"}]}
    out = {"slots": [{"name": "Fresh", "songs": [{"title": f"S{i}", "artist": f"A{i}", "reason": "new to you"}
                                                 for i in range(5)]}]}
    got = writer.slot_candidates(spec, {}, lambda p, i, d: out, per_slot=4)
    assert got["by"] == "llm" and len(got["candidates"]["Fresh"]) == 5
    legacy = {"slots": {"Fresh": out["slots"][0]["songs"]}}
    assert writer.slot_candidates(spec, {}, lambda p, i, d: legacy, per_slot=4)["by"] == "llm"
