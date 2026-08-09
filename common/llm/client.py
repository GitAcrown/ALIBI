"""Client OpenAI — wrapper minimal, appels ponctuels (pas de session/contexte conversationnel).

ALIBI n'a pas besoin d'un historique de conversation façon chatbot : chaque appel
(génération de dossier, audit, incarnation d'un suspect, résolution) est un échange
ponctuel et sans état, avec un prompt système + un prompt utilisateur construits par
l'appelant. Ce module est volontairement plus simple que son équivalent MARIA_R.
"""

import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI
import openai

logger = logging.getLogger("llm.client")

MODEL_MAIN = "gpt-5.6-luna"
# Modèle de repli si MODEL_MAIN renvoie une erreur de permissions (401).
MODEL_FALLBACK = "gpt-5.4-mini"

REQUEST_TIMEOUT = 90.0
MAX_RETRIES = 2


class LLMError(Exception):
    """Erreur LLM générique."""


class LLMOpenAIError(LLMError):
    """Erreur API OpenAI."""


class LLMClient:
    """Client unique pour l'API OpenAI — complétions structurées (JSON) ou texte libre."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL_MAIN,
        fallback_model: str = MODEL_FALLBACK,
    ):
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        self.model = model
        self.fallback_model = fallback_model

    async def chat(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        response_format: Optional[dict] = None,
        reasoning_effort: Optional[str] = "none",
    ) -> str:
        """Complétion chat simple, renvoie le texte de la réponse.

        `response_format` (optionnel) force une sortie structurée, ex.
        ``{"type": "json_schema", "json_schema": {...}}``.
        """
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            if kwargs["model"] == self.fallback_model:
                raise LLMOpenAIError(str(e)) from e
            logger.warning(
                "Modèle '%s' refusé (401) — repli sur '%s'.", kwargs["model"], self.fallback_model
            )
            kwargs["model"] = self.fallback_model
            try:
                completion = await self._client.chat.completions.create(**kwargs)
            except (openai.BadRequestError, openai.OpenAIError) as e2:
                raise LLMOpenAIError(str(e2)) from e2
        except (openai.BadRequestError, openai.OpenAIError) as e:
            raise LLMOpenAIError(str(e)) from e

        if not completion.choices:
            raise LLMOpenAIError("Complétion sans choix retournée par l'API.")
        content = completion.choices[0].message.content
        if not content:
            raise LLMOpenAIError("Complétion vide retournée par l'API.")
        return content

    async def chat_json(
        self,
        messages: list[dict],
        *,
        schema_name: str,
        json_schema: dict,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        reasoning_effort: Optional[str] = "none",
        strict: bool = True,
    ) -> dict:
        """Complétion contrainte par un JSON schema strict, renvoie déjà parsé (dict)."""
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": strict,
            },
        }
        raw = await self.chat(
            messages,
            model=model,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMOpenAIError(f"JSON invalide retourné par le modèle : {e}") from e

    async def close(self) -> None:
        await self._client.close()
