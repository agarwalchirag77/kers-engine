from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI
from pydantic import ValidationError

from .commitments import CommitmentAnalysis, analyze_commitments
from .config import DEFAULT_MODEL, PROMPT_VERSION, load_prompt_template
from .models import ConversationContext, SentimentResponse, TicketMetadata


KERS_SEED = 20260902
"""Fixed sampling seed. Combined with temperature=0 this makes the model's
scoring reproducible run-to-run — the same conversation should score the
same way twice. Before this, temperature=0.2 with no seed produced a
2.25-point ERS spread on the same ticket across two runs. Do not vary this
value: changing it re-randomises every future score."""


class AIClientError(Exception):
    pass


class OpenAIClient:
    """Wraps OpenAI Chat Completions with JSON response mode, validates
    against SentimentResponse, retries once on any failure.

    Commitment scoring is deterministic (engine-side) — we compute the
    analysis here from the raw messages, inject it into the prompt for the
    AI's contextual awareness, and OVERRIDE the AI's commitment-related
    fields in the response with our authoritative values.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        prompt_template: Optional[str] = None,
        prompt_version: str = PROMPT_VERSION,
        timeout_seconds: float = 30.0,
    ):
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)
        self.model = model
        self.prompt_template = prompt_template or load_prompt_template(prompt_version)
        self.prompt_version = prompt_version

    def _build_prompt(
        self,
        context: ConversationContext,
        metadata: TicketMetadata,
        analysis: CommitmentAnalysis,
        evaluation_time: datetime,
    ) -> str:
        prompt = self.prompt_template
        prompt = prompt.replace("{subject}", metadata.subject or "")
        prompt = prompt.replace("{channel}", metadata.channel or "")
        prompt = prompt.replace("{tags}", ", ".join(metadata.tags))
        prompt = prompt.replace("{locale}", metadata.locale or "")
        prompt = prompt.replace("{priority}", metadata.priority or "")
        prompt = prompt.replace("{formatted_messages}", context.formatted_messages)
        prompt = prompt.replace(
            "{commitment_analysis}", "\n  ".join(analysis.as_summary_lines())
        )
        prompt = prompt.replace(
            "{evaluation_time}",
            evaluation_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        return prompt

    async def score(
        self,
        context: ConversationContext,
        metadata: TicketMetadata,
        *,
        commitment_analysis: Optional[CommitmentAnalysis] = None,
        evaluation_time: Optional[datetime] = None,
    ) -> SentimentResponse:
        # Compute the analysis if the caller didn't pass one in. Tests and
        # the worker both inject explicitly; this fallback keeps the simpler
        # callers (e.g., the simulator) working without changes.
        if evaluation_time is None:
            evaluation_time = datetime.now(timezone.utc)
        if commitment_analysis is None:
            commitment_analysis = analyze_commitments(
                context.messages,
                evaluation_time=evaluation_time,
                channel=metadata.channel,
            )

        prompt = self._build_prompt(context, metadata, commitment_analysis, evaluation_time)

        last_error: Optional[str] = None
        for attempt in range(2):  # initial + 1 retry
            extra_messages = []
            if attempt > 0:
                extra_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Your previous response was rejected by validation. "
                            "Respond with VALID JSON only matching the schema "
                            "described in the system prompt. No prose outside "
                            "the JSON object."
                        ),
                    }
                )
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": prompt},
                        *extra_messages,
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    seed=KERS_SEED,
                )
                raw = response.choices[0].message.content or ""
                data = json.loads(raw)
                sentiment = SentimentResponse.model_validate(data)
                # Override commitment fields with our deterministic values.
                # The AI's value is informed by the analysis we showed it,
                # but the engine is ground truth.
                sentiment = sentiment.model_copy(
                    update={
                        "commitments_detected": commitment_analysis.detected,
                        "commitments_missed": commitment_analysis.missed,
                        "commitment_to_delivery_ratio": commitment_analysis.ratio_value,
                    }
                )
                return sentiment
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = f"validation: {e}"
                continue
            except Exception as e:
                last_error = f"network: {e}"
                continue

        raise AIClientError(f"AI scoring failed after retry: {last_error}")
