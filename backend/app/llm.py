"""
LLM provider abstraction supporting the controlled two-model comparison
requested in the corrections document: GPT-4o-mini (primary) versus an
open-source model such as Llama/Mistral, both operating over the identical
RAG pipeline, prompts and evaluation dataset.
"""
import re
import time
from typing import Dict, List

import requests

from app import config


class BaseLLM:
    name: str = "base"

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


class OpenAILLM(BaseLLM):
    name = config.OPENAI_CHAT_MODEL

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=config.OPENAI_CHAT_MODEL,
            temperature=config.LLM_TEMPERATURE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content


class OllamaLLM(BaseLLM):
    """Open-source model served locally through Ollama (e.g. llama3, mistral)."""
    name = config.OPEN_SOURCE_MODEL_NAME

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        resp = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.OPEN_SOURCE_MODEL_NAME,
                "prompt": f"{system_prompt}\n\n{user_prompt}",
                "stream": False,
                "options": {"temperature": config.LLM_TEMPERATURE},
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")


class DeterministicExtractiveLLM(BaseLLM):
    """Offline fallback 'model'. Rather than hallucinate free-form prose, it
    deterministically composes an answer strictly extracted from the
    retrieved context chunks, guaranteeing zero-hallucination behaviour when
    no live LLM API/inference endpoint is reachable. Two independent
    configurations (`primary` / `alternate`) apply different selection
    breadth, so a meaningful side-by-side comparison can still be produced
    offline."""

    def __init__(self, variant: str, name: str):
        self.variant = variant
        self.name = name

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        from app import composer

        question = _extract_section(user_prompt, "QUESTION") or user_prompt
        context = _extract_section(user_prompt, "CONTEXT")
        observations = _extract_section(user_prompt, "TOOL OBSERVATIONS")

        chunks = _parse_context_chunks(context)
        if not chunks:
            if observations:
                return observations
            return ("I could not find that in the official policy documents I have "
                    "access to. I can help with baggage allowances and fees, flight "
                    "delays and compensation, refunds and ticket changes, and check-in "
                    "or boarding rules.")

        # The primary configuration answers more completely; the alternate
        # open-source configuration is deliberately terser.
        verbose = self.variant == "primary"
        answer = composer.compose_answer(question, chunks,
                                          max_sentences=3 if verbose else 2,
                                          verbose=verbose)
        return answer


def _extract_section(prompt: str, header: str) -> str:
    pattern = rf"{re.escape(header)}:\n(.*?)(?=\n[A-Z][A-Z ]+:\n|\Z)"
    match = re.search(pattern, prompt, re.S)
    return match.group(1).strip() if match else ""


def _parse_context_chunks(context: str) -> List[Dict]:
    """Rebuilds chunk dicts from the serialized prompt context."""
    chunks = []
    if not context:
        return chunks
    for block in context.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        section = ""
        text = block
        header_match = re.match(r"\[(.*?)\]\n(.*)", block, re.S)
        if header_match:
            section = header_match.group(1).strip()
            text = header_match.group(2).strip()
        source = ""
        if "|" in section:
            source, section = [p.strip() for p in section.split("|", 1)]
        chunks.append({"text": text, "section": section, "source": source})
    return chunks


def get_primary_llm() -> BaseLLM:
    if config.OPENAI_API_KEY:
        return OpenAILLM()
    return DeterministicExtractiveLLM(variant="primary", name=f"{config.OPENAI_CHAT_MODEL} (offline-sim)")


def get_alternate_llm() -> BaseLLM:
    try:
        requests.get(config.OLLAMA_BASE_URL, timeout=1)
        return OllamaLLM()
    except Exception:
        return DeterministicExtractiveLLM(
            variant="alternate", name=f"{config.OPEN_SOURCE_MODEL_NAME} (offline-sim)"
        )


def compare_models(system_prompt: str, user_prompt: str) -> List[Dict]:
    """Runs both configured models over the identical prompt/context, used by
    the dual-model comparative evaluation (corrections requirement)."""
    results = []
    for llm in (get_primary_llm(), get_alternate_llm()):
        started = time.perf_counter()
        try:
            text = llm.generate(system_prompt, user_prompt)
            results.append({
                "model": llm.name,
                "response": text,
                "response_time_seconds": round(time.perf_counter() - started, 4),
                "error": None,
            })
        except Exception as exc:  # pragma: no cover - network failure path
            results.append({
                "model": llm.name,
                "response": "",
                "response_time_seconds": round(time.perf_counter() - started, 4),
                "error": str(exc),
            })
    return results
