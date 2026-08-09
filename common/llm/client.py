"""Client OpenAI — wrapper minimal, appels ponctuels (pas de session/contexte conversationnel).

ALIBI n'a pas besoin d'un historique de conversation façon chatbot : chaque appel
(génération de dossier, audit, incarnation d'un suspect, résolution) est un échange
ponctuel et sans état, avec un prompt système + un prompt utilisateur construits par
l'appelant. Ce module est volontairement plus simple que son équivalent MARIA_R.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI
import openai

logger = logging.getLogger("llm.client")

MODEL_MAIN = "gpt-5.6-luna"
# Modèle de repli si MODEL_MAIN renvoie une erreur de permissions (401).
MODEL_FALLBACK = "gpt-5.4-mini"

REQUEST_TIMEOUT = 90.0
MAX_RETRIES = 2

# Début du champ string "reponse" dans un JSON partiel streamé.
_REPONSE_KEY_RE = re.compile(r'"reponse"\s*:\s*"')


def extract_partial_reponse(buffer: str) -> Optional[str]:
    """Extrait la valeur (éventuellement encore incomplète) du champ JSON `reponse`.

    Sert au streaming Discord : on n'attend pas la fin du JSON pour afficher ce que
    le suspect a déjà « dit ». Renvoie None tant que le champ n'a pas commencé.
    """
    m = _REPONSE_KEY_RE.search(buffer)
    if not m:
        return None
    i = m.end()
    chars: list[str] = []
    while i < len(buffer):
        c = buffer[i]
        if c == "\\":
            if i + 1 >= len(buffer):
                break  # escape incomplet — on s'arrête avant
            nxt = buffer[i + 1]
            if nxt == "u":
                # \uXXXX — attend 4 hex ; sinon buffer encore trop court.
                if i + 6 > len(buffer):
                    break
                hexpart = buffer[i + 2 : i + 6]
                if all(ch in "0123456789abcdefABCDEF" for ch in hexpart):
                    chars.append(chr(int(hexpart, 16)))
                    i += 6
                    continue
                break
            escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
            chars.append(escapes.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            break  # fin de string
        chars.append(c)
        i += 1
    return "".join(chars)


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

    def _chat_kwargs(
        self,
        messages: list[dict],
        *,
        model: Optional[str],
        max_tokens: int,
        response_format: Optional[dict],
        reasoning_effort: Optional[str],
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if stream:
            kwargs["stream"] = True
        return kwargs

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
        kwargs = self._chat_kwargs(
            messages,
            model=model,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )

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

    async def _stream_text(
        self,
        kwargs: dict[str, Any],
        *,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> str:
        """Lit un stream chat.completions et renvoie le texte agrégé.

        `on_delta` est appelé à chaque morceau avec le buffer cumulé (pas seulement
        le delta) — l'appelant décide s'il throttle les mises à jour UI.
        """
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        try:
            stream = await self._client.chat.completions.create(**stream_kwargs)
        except openai.AuthenticationError as e:
            if stream_kwargs["model"] == self.fallback_model:
                raise LLMOpenAIError(str(e)) from e
            logger.warning(
                "Modèle '%s' refusé (401) — repli sur '%s'.",
                stream_kwargs["model"], self.fallback_model,
            )
            stream_kwargs["model"] = self.fallback_model
            try:
                stream = await self._client.chat.completions.create(**stream_kwargs)
            except (openai.BadRequestError, openai.OpenAIError) as e2:
                raise LLMOpenAIError(str(e2)) from e2
        except (openai.BadRequestError, openai.OpenAIError) as e:
            raise LLMOpenAIError(str(e)) from e

        buffer = ""
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            buffer += delta
            if on_delta is not None:
                try:
                    await on_delta(buffer)
                except Exception:
                    logger.exception("on_delta a échoué (ignoré — le stream continue)")
        if not buffer:
            raise LLMOpenAIError("Complétion vide retournée par l'API.")
        return buffer

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
        on_partial_reponse: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> dict:
        """Complétion contrainte par un JSON schema strict, renvoie déjà parsé (dict).

        Si `on_partial_reponse` est fourni, stream la complétion et appelle le callback
        chaque fois que le champ `reponse` (string) progresse — utile pour afficher
        la déposition d'un suspect en direct. En cas d'échec du stream, repli sur
        l'appel non-streamé (sans callback).
        """
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": strict,
            },
        }
        kwargs = self._chat_kwargs(
            messages,
            model=model,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )

        if on_partial_reponse is not None:
            last_shown = ""

            async def _on_delta(buffer: str) -> None:
                nonlocal last_shown
                partial = extract_partial_reponse(buffer)
                if partial is None or partial == last_shown:
                    return
                last_shown = partial
                await on_partial_reponse(partial)

            try:
                raw = await self._stream_text(kwargs, on_delta=_on_delta)
            except LLMOpenAIError:
                logger.warning(
                    "Stream JSON indisponible — repli sur complétion non-streamée."
                )
                raw = await self.chat(
                    messages,
                    model=model,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                )
        else:
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
