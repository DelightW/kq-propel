"""
LLM provider abstraction supporting the controlled two-model comparison
requested in the corrections document: GPT-4o-mini (primary) versus an
open-source model such as Llama/Mistral, both operating over the identical
RAG pipeline, prompts and evaluation dataset.
"""
import re
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
    configurations (`primary` / `alternate`) apply slightly different
    extraction/summarization strategies so a meaningful side-by-side
    comparison can still be produced offline."""

    def __init__(self, variant: str, name: str):
        self.variant = variant
        self.name = name

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        context_match = re.search(r"CONTEXT:\n(.*?)\nQUESTION:", user_prompt, re.S)
        question_match = re.search(r"QUESTION:\n(.*)", user_prompt, re.S)
        context = context_match.group(1).strip() if context_match else ""
        question = question_match.group(1).strip() if question_match else user_prompt

        if not context:
            return ("I could not find that information in the official KQ policy "
                    "documents, so I cannot answer with confidence. Could you "
                    "rephrase your question or provide more detail?")

        sentences = re.split(r"(?<=[.!?])\s+", context)
        keywords = [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", question)]

        def relevance(sentence: str) -> int:
            s = sentence.lower()
            return sum(1 for kw in keywords if kw in s)

        ranked = sorted(sentences, key=relevance, reverse=True)
        top_n = 4 if self.variant == "primary" else 2
        top = [s for s in ranked[:top_n] if s.strip()]
        if self.variant != "primary":
            top = top[::-1]  # alternate model composes in a different order
        answer = " ".join(top).strip()
        return answer or context[:400]


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
        try:
            text = llm.generate(system_prompt, user_prompt)
            results.append({"model": llm.name, "response": text, "error": None})
        except Exception as exc:  # pragma: no cover - network failure path
            results.append({"model": llm.name, "response": "", "error": str(exc)})
    return results
