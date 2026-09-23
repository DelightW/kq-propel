"""Isolated verification of audit findings. Read-only: no DB or file mutations."""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import aviationstack, config, evaluation, llm, vectorstore  # noqa: E402

print("=" * 72)
print("D4  AviationStack live/sandbox key contract")
print("=" * 72)
sandbox = aviationstack.get_flight_status("KQ100")
live_record = {
    "flight_status": "active",
    "departure": {"airport": "Jomo Kenyatta International", "iata": "NBO",
                  "gate": "B14", "delay": 185,
                  "estimated": "2026-09-23T11:25:00+00:00"},
    "arrival": {"airport": "Heathrow", "iata": "LHR"},
}
live = aviationstack.normalize_live_flight(live_record, "KQ100")
agent_reads = ["status", "route", "gate", "delay_minutes"]
missing = [k for k in agent_reads if k not in live]
print("sandbox keys :", sorted(sandbox))
print("live keys    :", sorted(live))
print("agent reads  :", agent_reads)
print("MISSING live :", missing or "none")
print("live route   :", live["route"])
print("live gate    :", live["gate"], "| delay:", live["delay_minutes"])
d4_ok = not missing and set(live) == set(sandbox)
print("VERDICT      :", "FIXED - both paths satisfy one declared contract."
      if d4_ok else "DEFECT - live responses degrade to n/a")

print()
print("=" * 72)
print("D5  Dual-generator comparison labelling")
print("=" * 72)
p, a = llm.get_primary_llm(), llm.get_alternate_llm()
mode = llm.comparison_mode()
print("primary      :", p.name)
print("alternate    :", a.name)
print("same class   :", type(p) is type(a))
sys_p = "You are a helpful airline assistant."
usr = ("QUESTION:\nHow much is the overweight baggage fee for a bag 15kg over?\n\n"
       "CONTEXT:\n[baggage_policy | Section 2: Overweight Baggage Fees]\n"
       "Bags between 11kg and 20kg over the limit incur a flat fee of Ksh 9,000.\n")
identical = p.generate(sys_p, usr) == a.generate(sys_p, usr)
print("identical    :", identical)
print("reported mode:", mode["mode"])
print("panel title  :", mode["title"])
print("claims model comparison:", mode["is_model_comparison"])
# The run is honest if it never claims to be a model comparison while both
# generators are simulated.
d5_ok = (not mode["is_model_comparison"]) if (p.is_simulated or a.is_simulated) else True
d5_ok = d5_ok and bool(mode["caveat"]) if (p.is_simulated and a.is_simulated) else d5_ok
print("VERDICT      :", "FIXED - the run self-describes as an ablation and carries"
      if d5_ok else "STILL MISLABELLED")
if d5_ok:
    print("               a caveat; it no longer impersonates named LLMs.")

print()
print("=" * 72)
print("D1  Groundedness - fabrication and contradiction rejection")
print("=" * 72)
ctx = [{"text": "Bags between 11kg and 20kg over the limit incur a flat fee of Ksh 9,000.",
        "section": "Section 2", "source": "baggage_policy", "score": 0.9}]
source = ctx[0]["text"]
empathy = ("I'm really sorry for the trouble, I'll help you right away. " + source)
cases = [
    ("verbatim extract (correct)", source, "high"),
    ("paraphrase (correct)", "You will pay nine thousand shillings for that bag.", "high"),
    ("empathy preamble + correct", empathy, "high"),
    ("FABRICATED figure", "The fee is Ksh 12,000 payable at the gate by card.", "low"),
    ("contradicts source", "There is no fee for bags over the limit.", "low"),
]
d1_ok = True
for label, ans, want in cases:
    got = evaluation.groundedness(ans, source)
    ok = got >= 0.7 if want == "high" else got < 0.3
    d1_ok &= ok
    print(f"  {label:.<32} {got:<6} expect {want:<5} {'OK' if ok else 'FAIL'}")
print("VERDICT      :", "FIXED - claim-level verification rejects fabrication and"
      if d1_ok else "STILL BROKEN")
if d1_ok:
    print("               contradiction while accepting correct paraphrase.")

print()
print("=" * 72)
print("D2  context_relevance - bounded to [0,1]")
print("=" * 72)
res = vectorstore.get_vector_store().similarity_search("overweight baggage fee", k=4)
ranking = [round(c.get("score", 0.0), 4) for c in res]
cosines = [round(c.get("dense_score", 0.0), 4) for c in res]
metric = evaluation.context_relevance(res)
print("ranking scores (unbounded, by design):", ranking)
print("query-chunk cosines                  :", cosines)
print("context_relevance metric             :", metric)
d2_ok = 0.0 <= metric <= 1.0
print("VERDICT      :", "FIXED - metric reads the cosine, not the ranking blend;"
      if d2_ok else "STILL BROKEN")
if d2_ok:
    print("               ranking and measurement are now separated.")

print()
print("=" * 72)
print("D3  answer_relevance - resolution over restatement")
print("=" * 72)
q = "How much is the overweight baggage fee for a bag that is 15kg over the limit?"
good = "Bags between 11kg and 20kg over the limit incur a flat fee of Ksh 9,000."
echo = "How much is the overweight baggage fee for a bag that is 15kg over the limit"
gold = ["9,000"]
g_gold, e_gold = (evaluation.answer_relevance(good, q, gold),
                  evaluation.answer_relevance(echo, q, gold))
g_fb, e_fb = evaluation.answer_relevance(good, q), evaluation.answer_relevance(echo, q)
print(f"  with gold keys : answer {g_gold}  echo {e_gold}")
print(f"  fallback path  : answer {g_fb}  echo {e_fb}")
d3_ok = g_gold > e_gold and g_fb > e_fb
print("VERDICT      :", "FIXED - the answer now outscores the echo on both paths."
      if d3_ok else "STILL BROKEN")

print()
print("=" * 72)
print("D6  Credential posture")
print("=" * 72)
for key in ("OPENAI_API_KEY", "AVIATIONSTACK_API_KEY", "DARAJA_CONSUMER_KEY"):
    print(f"  {key:.<26} {'set' if getattr(config, key, '') else 'EMPTY -> fallback'}")
print("  .env present ............. ", (BACKEND.parent / ".env").exists())
