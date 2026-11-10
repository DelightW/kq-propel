"""
LLM provider abstraction supporting the controlled two-model comparison
requested in the corrections document: GPT-4o-mini (primary) versus an
open-source model such as Llama/Mistral, both operating over the identical
RAG pipeline, prompts and evaluation dataset.
"""
import re
import time
from typing import Dict, List, Optional

import requests

from app import config


class BaseLLM:
    name: str = "base"
    is_simulated: bool = False

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


class OpenAILLM(BaseLLM):
    name = config.OPENAI_CHAT_MODEL

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=config.LLM_TIMEOUT_SECONDS,
                        max_retries=1)
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
        # The chat endpoint keeps the system prompt in the system role, as the
        # OpenAI side does, so both models receive the same instructions in
        # the same form.
        resp = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/chat",
            json={
                "model": config.OPEN_SOURCE_MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": config.LLM_TEMPERATURE},
            },
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return (resp.json().get("message") or {}).get("content", "")


class DeterministicExtractiveLLM(BaseLLM):
    """Offline fallback 'model'. Rather than hallucinate free-form prose, it
    deterministically composes an answer strictly extracted from the retrieved
    context chunks, guaranteeing zero-hallucination behaviour when no live LLM
    API or inference endpoint is reachable.

    Two configurations (`primary` / `alternate`) differ only in selection
    breadth. They are NOT stand-ins for GPT-4o-mini and Llama-3: they share one
    code path and, on many queries, emit byte-identical text. Comparing them is
    a response-breadth ablation, not a model comparison, and is labelled as
    such throughout the API and dashboard."""

    is_simulated = True

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


def _offline_primary() -> BaseLLM:
    return DeterministicExtractiveLLM(
        variant="primary", name="extractive-broad (offline, not an LLM)")


def get_primary_llm() -> BaseLLM:
    """The model that answers passengers, chosen by CHAT_MODEL_PROVIDER."""
    provider = config.CHAT_MODEL_PROVIDER
    if provider == "ollama":
        return OllamaLLM()
    if provider in ("openai", "auto") and config.OPENAI_API_KEY:
        return OpenAILLM()
    return _offline_primary()


def check_live_answer(answer: str, user_prompt: str) -> Optional[str]:
    """Why a live model's answer must not be shown, or None if it may.

    The offline composer can only quote policy text. A live model can write
    anything, so before its answer reaches a passenger it must state no
    quantity - a fee, a weight, a deadline - that is absent from the context,
    the tool observations and the question it was given. This is the same
    quantity test the groundedness metric applies.
    """
    from app import evaluation

    text = (answer or "").strip()
    if not text:
        return "empty answer"
    # Citation lines carry edition dates, which are attribution, not claims.
    claims = "\n".join(line for line in text.splitlines()
                       if not line.strip().lower().startswith("source"))
    invented = evaluation._numeric_atoms(claims) - evaluation._numeric_atoms(user_prompt)
    if invented:
        shown = ", ".join(f"{n:g}" for n in sorted(invented)[:3])
        return f"states figures not in the policy context ({shown})"
    return None


def generate_answer(system_prompt: str, user_prompt: str) -> Dict:
    """Answers with the configured model, falling back to the offline composer
    when the model is unreachable, too slow, or states an unsupported figure.

    Returns {"answer", "model", "fallback"}; `fallback` is the reason the live
    answer was not used, or None.
    """
    llm = get_primary_llm()
    if llm.is_simulated:
        return {"answer": llm.generate(system_prompt, user_prompt), "model": llm.name,
                "fallback": None}
    try:
        answer = llm.generate(system_prompt, user_prompt)
    except Exception as exc:  # network failure, timeout, quota, bad model name
        reason = f"{llm.name} was unavailable ({type(exc).__name__})"
    else:
        problem = check_live_answer(answer, user_prompt)
        if problem is None:
            return {"answer": answer.strip(), "model": llm.name, "fallback": None}
        reason = f"{llm.name} answer was withheld: it {problem}"
    offline = _offline_primary()
    return {"answer": offline.generate(system_prompt, user_prompt), "model": offline.name,
            "fallback": reason}


def get_alternate_llm() -> BaseLLM:
    try:
        requests.get(config.OLLAMA_BASE_URL, timeout=1)
        return OllamaLLM()
    except Exception:
        return DeterministicExtractiveLLM(
            variant="alternate", name="extractive-concise (offline, not an LLM)")


def comparison_mode() -> Dict:
    """Describes what a comparison run would actually be comparing, so the API
    and dashboard can label it honestly instead of implying a model study that
    the current credential posture cannot support."""
    primary, alternate = get_primary_llm(), get_alternate_llm()
    simulated = [m for m in (primary, alternate) if m.is_simulated]

    if not simulated:
        return {
            "mode": "live_model_comparison",
            "title": "Controlled Dual-Model Comparison",
            "description": ("Two distinct models over identical documents, chunking, "
                            "embeddings, retrieval and prompts - only the model varies."),
            "is_model_comparison": True,
            "caveat": None,
            "models": {"primary": primary.name, "alternate": alternate.name},
        }

    if len(simulated) == 2:
        return {
            "mode": "offline_breadth_ablation",
            "title": "Response-Breadth Ablation (offline)",
            "description": ("No LLM endpoint is reachable, so both columns are the same "
                            "deterministic extractive composer differing only in how many "
                            "sentences it selects."),
            "is_model_comparison": False,
            "caveat": ("This is NOT a GPT-4o-mini vs Llama-3 comparison. Both columns "
                       "share one code path and frequently produce identical text. "
                       "Set OPENAI_API_KEY and/or run Ollama to obtain a genuine "
                       "model comparison."),
            "models": {"primary": primary.name, "alternate": alternate.name},
        }

    return {
        "mode": "partial_model_comparison",
        "title": "Partial Model Comparison",
        "description": "One live model is compared against the offline extractive composer.",
        "is_model_comparison": False,
        "caveat": ("Only one side is a live model; the other is the deterministic "
                   "extractive fallback, so differences conflate model and method."),
        "models": {"primary": primary.name, "alternate": alternate.name},
    }


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
