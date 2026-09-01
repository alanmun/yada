"""The transformation pipeline.

The governing rule under test: a failure never loses the transcript.
"""

from __future__ import annotations

import pytest

from yada.config import TransformStep, Vocabulary
from yada.pipeline.transform import (
    DEFAULT_SYSTEM_PROMPT,
    apply_find_replace,
    build_system_prompt,
    build_user_prompt,
    run_steps,
    vocabulary_block,
)
from yada.providers.base import TransformOptions, TransformResult


class FakeTransformer:
    """Records what it was asked, returns what it was told to."""

    id = "fake"
    label = "Fake"

    def __init__(self, reply="cleaned up", *, fail=None, tokens=(10, 5)):
        self.reply = reply
        self.fail = fail
        self.tokens = tokens
        self.calls: list[tuple[str, str]] = []

    def capabilities(self, model=None):
        from yada.providers.base import TransformCapabilities

        return TransformCapabilities()

    async def list_models(self):
        return []

    async def transform(self, system, user, opts):
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError(self.fail)
        return TransformResult(
            text=self.reply,
            model=opts.model,
            provider="fake",
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            cost_usd=0.0001,
        )


OPTS = TransformOptions(model="gpt-5.6-luna")


# --------------------------------------------------------------------------------------
# find_replace
# --------------------------------------------------------------------------------------


def test_literal_find_replace():
    step = TransformStep(type="find_replace", find="trout wood", replace="Troutwood")
    text, err = apply_find_replace(step, "I work at trout wood today")
    assert text == "I work at Troutwood today"
    assert err is None


def test_regex_find_replace():
    step = TransformStep(type="find_replace", find=r"\bum\b,?\s*", replace="", use_regex=True)
    text, err = apply_find_replace(step, "so um, I think um it works")
    assert text == "so I think it works"
    assert err is None


def test_invalid_regex_reports_but_does_not_lose_text():
    step = TransformStep(type="find_replace", find="([unclosed", replace="x", use_regex=True)
    text, err = apply_find_replace(step, "original text")
    assert text == "original text", "a broken pattern must not discard the transcript"
    assert err and "invalid regular expression" in err


def test_empty_find_is_a_noop():
    step = TransformStep(type="find_replace", find="", replace="x")
    assert apply_find_replace(step, "unchanged") == ("unchanged", None)


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


def test_vocabulary_block_lists_terms():
    block = vocabulary_block(Vocabulary(terms=["Troutwood", "DynamoDB", "  "]))
    assert "- Troutwood" in block and "- DynamoDB" in block
    assert "  \n" not in block.replace("- ", ""), "blank terms should be dropped"


def test_vocabulary_block_empty_when_no_terms():
    assert vocabulary_block(Vocabulary(terms=[])) == ""


def test_vocabulary_appended_to_system_prompt_by_default():
    step = TransformStep(system_prompt="Fix the grammar.")
    prompt = build_system_prompt(step, Vocabulary(terms=["Munirji"]))
    assert prompt.startswith("Fix the grammar.")
    assert "Munirji" in prompt


def test_vocabulary_placeholder_controls_position():
    step = TransformStep(system_prompt="BEFORE\n{{vocabulary}}\nAFTER")
    prompt = build_system_prompt(step, Vocabulary(terms=["Yada"]))
    assert prompt.index("BEFORE") < prompt.index("Yada") < prompt.index("AFTER")


def test_default_system_prompt_used_when_blank():
    assert build_system_prompt(TransformStep(system_prompt=""), Vocabulary()) == (
        DEFAULT_SYSTEM_PROMPT
    )


def test_user_prompt_placeholder_substitution():
    step = TransformStep(user_prompt_template="Clean this:\n{{input}}")
    assert build_user_prompt(step, "hello") == "Clean this:\nhello"


def test_user_prompt_without_placeholder_still_includes_text():
    """A template that forgets {{input}} must not send an empty transcript."""
    step = TransformStep(user_prompt_template="Just clean it up")
    assert "hello" in build_user_prompt(step, "hello")


# --------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------


async def test_steps_run_in_order():
    steps = [
        TransformStep(type="find_replace", find="a", replace="b"),
        TransformStep(type="find_replace", find="b", replace="c"),
    ]
    out = await run_steps("aaa", steps, provider=None, options=None, vocabulary=Vocabulary())
    assert out.text == "ccc", "second step must see the first step's output"
    assert out.ok


async def test_disabled_steps_are_skipped():
    steps = [
        TransformStep(type="find_replace", find="a", replace="b", enabled=False),
        TransformStep(type="find_replace", find="a", replace="z"),
    ]
    out = await run_steps("aaa", steps, provider=None, options=None, vocabulary=Vocabulary())
    assert out.text == "zzz"
    assert out.steps[0].skipped is True


async def test_prompt_transform_receives_vocabulary_and_text():
    fake = FakeTransformer(reply="Polished.")
    steps = [TransformStep(type="prompt_transform", system_prompt="Fix it.")]
    out = await run_steps(
        "raw text", steps, provider=fake, options=OPTS, vocabulary=Vocabulary(terms=["Troutwood"])
    )
    assert out.text == "Polished."
    system, user = fake.calls[0]
    assert "Troutwood" in system
    assert "raw text" in user
    assert out.total_cost_usd == pytest.approx(0.0001)


async def test_failed_llm_step_preserves_prior_text():
    steps = [
        TransformStep(type="find_replace", find="raw", replace="fixed"),
        TransformStep(type="prompt_transform"),
    ]
    fake = FakeTransformer(fail="rate limited")
    out = await run_steps("raw text", steps, provider=fake, options=OPTS, vocabulary=Vocabulary())
    assert out.text == "fixed text", "must return the text as of before the failed step"
    assert out.ok is False
    assert "rate limited" in (out.first_error or "")


async def test_empty_model_output_is_a_failure_not_a_result():
    """An empty completion must not silently delete the dictation."""
    fake = FakeTransformer(reply="   ")
    steps = [TransformStep(type="prompt_transform")]
    out = await run_steps("my words", steps, provider=fake, options=OPTS, vocabulary=Vocabulary())
    assert out.text == "my words"
    assert out.ok is False
    assert "returned nothing" in (out.first_error or "")


async def test_prompt_transform_without_provider_is_reported():
    steps = [TransformStep(type="prompt_transform")]
    out = await run_steps("text", steps, provider=None, options=None, vocabulary=Vocabulary())
    assert out.text == "text"
    assert "no transform provider" in (out.first_error or "")


async def test_unknown_step_type_is_reported():
    steps = [TransformStep(type="teleport")]
    out = await run_steps("text", steps, provider=None, options=None, vocabulary=Vocabulary())
    assert out.text == "text"
    assert "unknown step type" in (out.first_error or "")


async def test_no_steps_returns_input_unchanged():
    out = await run_steps("text", [], provider=None, options=None, vocabulary=Vocabulary())
    assert out.text == "text"
    assert out.ok and not out.changed
