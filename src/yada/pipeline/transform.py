"""The transformation pipeline: an ordered list of steps applied to a transcript.

Two step types, borrowed from Whispering because the design is sound:

* `find_replace` -- literal or regex substitution. Runs locally, costs nothing, and cannot
  be talked out of it by a model. The right tool for a misspelling that keeps coming back.
* `prompt_transform` -- an LLM pass. Good at grammar, tone and structure; unreliable at
  remembering that your colleague's name has two Ns.

Steps compose, so "fix my known spellings deterministically, then clean up the grammar" is
expressible without either half special-casing the other.

The governing rule: **a failed step never loses the transcript.** If a step errors, the
pipeline returns the text as it stood before that step and reports what went wrong. Nobody
should lose what they just said because a model was rate-limited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import TransformStep, Vocabulary
from ..providers.base import TransformOptions, TransformProvider

INPUT_PLACEHOLDER = "{{input}}"
VOCABULARY_PLACEHOLDER = "{{vocabulary}}"

DEFAULT_SYSTEM_PROMPT = (
    "You clean up dictated speech. Fix grammar, punctuation and capitalisation, remove "
    "filler words and false starts, and keep the speaker's wording and meaning intact. "
    "Do not answer questions, add commentary, or summarise. Return only the corrected text."
)


@dataclass(slots=True)
class StepOutcome:
    index: int
    type: str
    ok: bool
    output: str
    error: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    skipped: bool = False


@dataclass(slots=True)
class TransformOutcome:
    text: str
    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def first_error(self) -> str | None:
        return next((s.error for s in self.steps if s.error), None)

    @property
    def total_cost_usd(self) -> float | None:
        costs = [s.cost_usd for s in self.steps if s.cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def changed(self) -> bool:
        return any(s.ok and not s.skipped for s in self.steps)


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


def vocabulary_block(vocab: Vocabulary) -> str:
    """Spelling guidance for an LLM prompt.

    The same terms also go to the transcription model's native `keywords` field where one
    exists, which is the more effective of the two -- the transcriber can use them while
    decoding audio, whereas a transform pass can only repair what it already received. This
    is the belt to that braces: it catches terms the transcriber still got wrong.
    """
    terms = [t.strip() for t in vocab.terms if t.strip()]
    if not terms:
        return ""
    listed = "\n".join(f"- {t}" for t in terms)
    return (
        "These terms appear in this speaker's dictation and must be spelled exactly as "
        f"written here:\n{listed}\n"
        "Correct any near-miss spelling of these terms. Do not otherwise substitute words."
    )


def build_system_prompt(step: TransformStep, vocab: Vocabulary) -> str:
    """Assemble the system prompt, injecting vocabulary.

    An explicit {{vocabulary}} placeholder wins, so a hand-written prompt can put the terms
    wherever it wants. Otherwise the block is appended, which is the sensible default.
    """
    base = step.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    block = vocabulary_block(vocab)
    if VOCABULARY_PLACEHOLDER in base:
        return base.replace(VOCABULARY_PLACEHOLDER, block).strip()
    if not block:
        return base
    return f"{base}\n\n{block}"


def build_user_prompt(step: TransformStep, text: str) -> str:
    template = step.user_prompt_template or INPUT_PLACEHOLDER
    if INPUT_PLACEHOLDER not in template:
        # A template that forgets the placeholder would silently send an empty transcript.
        # Appending is a better guess than failing.
        return f"{template}\n\n{text}".strip()
    return template.replace(INPUT_PLACEHOLDER, text)


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def apply_find_replace(step: TransformStep, text: str) -> tuple[str, str | None]:
    """Literal or regex substitution. Returns (text, error)."""
    if not step.find:
        return text, None
    if not step.use_regex:
        return text.replace(step.find, step.replace), None
    try:
        pattern = re.compile(step.find)
    except re.error as exc:
        # Keep going with unmodified text: one bad pattern in settings should not block
        # every future dictation.
        return text, f"invalid regular expression: {exc}"
    try:
        return pattern.sub(step.replace, text), None
    except re.error as exc:
        return text, f"replacement failed: {exc}"


async def run_steps(
    text: str,
    steps: list[TransformStep],
    *,
    provider: TransformProvider | None,
    options: TransformOptions | None,
    vocabulary: Vocabulary,
) -> TransformOutcome:
    """Run the pipeline. Never raises; failures are recorded per step.

    Returns as soon as a step fails, carrying the text from before that step -- the
    alternative, feeding a half-transformed string into the next step, produces worse output
    than stopping.
    """
    outcome = TransformOutcome(text=text)
    current = text

    for index, step in enumerate(steps):
        if not step.enabled:
            outcome.steps.append(
                StepOutcome(index=index, type=step.type, ok=True, output=current, skipped=True)
            )
            continue

        if step.type == "find_replace":
            updated, error = apply_find_replace(step, current)
            outcome.steps.append(
                StepOutcome(
                    index=index,
                    type=step.type,
                    ok=error is None,
                    output=updated,
                    error=error,
                )
            )
            if error:
                outcome.text = current
                return outcome
            current = updated
            continue

        if step.type == "prompt_transform":
            if provider is None or options is None:
                outcome.steps.append(
                    StepOutcome(
                        index=index,
                        type=step.type,
                        ok=False,
                        output=current,
                        error="no transform provider is configured",
                    )
                )
                outcome.text = current
                return outcome
            system = build_system_prompt(step, vocabulary)
            user = build_user_prompt(step, current)
            try:
                result = await provider.transform(system, user, options)
            except Exception as exc:  # noqa: BLE001 - provider errors are step failures
                outcome.steps.append(
                    StepOutcome(
                        index=index, type=step.type, ok=False, output=current, error=str(exc)[:300]
                    )
                )
                outcome.text = current
                return outcome
            produced = result.text.strip()
            if not produced:
                # An empty completion is a failure, not a result. Returning it would silently
                # delete the user's dictation.
                outcome.steps.append(
                    StepOutcome(
                        index=index,
                        type=step.type,
                        ok=False,
                        output=current,
                        error="the model returned nothing",
                        model=result.model,
                    )
                )
                outcome.text = current
                return outcome
            outcome.steps.append(
                StepOutcome(
                    index=index,
                    type=step.type,
                    ok=True,
                    output=produced,
                    model=result.model,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                )
            )
            current = produced
            continue

        outcome.steps.append(
            StepOutcome(
                index=index,
                type=step.type,
                ok=False,
                output=current,
                error=f"unknown step type {step.type!r}",
            )
        )
        outcome.text = current
        return outcome

    outcome.text = current
    return outcome


def default_steps() -> list[TransformStep]:
    """What a new install gets when the user first enables transforms."""
    return [TransformStep(type="prompt_transform", system_prompt=DEFAULT_SYSTEM_PROMPT)]
