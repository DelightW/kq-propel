"""The policy index is rebuilt when the policies change.

Until 10 November only a change of embedding width triggered a rebuild, so a
local index kept serving 52 chunks after a section had been added (53). An
edited policy or a new edition would have been answered from the old text.

Runs on a copy of the policy folder in a temporary directory.
Run:  python tools/test_index_freshness.py
"""
import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

TMP = Path(tempfile.mkdtemp())
POLICIES = TMP / "policies"
shutil.copytree(config.POLICY_DIR, POLICIES)
config.VECTOR_STORE_PATH = TMP / "vector_store.json"

from app import vectorstore  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" :: {detail}" if detail and not cond else ""))


def ingest():
    importlib.reload(vectorstore)          # a fresh process, as at server start
    vectorstore.config.VECTOR_STORE_PATH = TMP / "vector_store.json"
    return vectorstore.ingest_policy_directory(POLICIES)["chunks"]


sections = sum(p.read_text(encoding="utf-8").count("\nSection ") + p.read_text(encoding="utf-8").startswith("Section ")
               for p in POLICIES.glob("*.txt"))
first = ingest()
check("first build indexes every section", first == sections, f"{first} chunks, {sections} sections")
check("an unchanged corpus reuses the saved index", ingest() == first)

policy = POLICIES / "checkin_policy.txt"
policy.write_text(policy.read_text(encoding="utf-8") + "\n\nSection 9: Test Rule\nA rule added by the test.\n",
                  encoding="utf-8")
check("an added section triggers a rebuild", ingest() == first + 1)

manifest = POLICIES / "provenance.json"
data = json.loads(manifest.read_text(encoding="utf-8"))
before = vectorstore.corpus_fingerprint(POLICIES)
data["test_edition_change"] = "2.0"
manifest.write_text(json.dumps(data), encoding="utf-8")
check("a changed provenance manifest changes the fingerprint", vectorstore.corpus_fingerprint(POLICIES) != before)

print(f"\n{len(passed)}/{len(passed) + len(failed)} passed")
sys.exit(1 if failed else 0)
