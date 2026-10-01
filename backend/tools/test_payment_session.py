"""Multi-turn payment conversation over the authenticated session layer.

Payment state is keyed on the session identifier, so replacing client-chosen
identifiers with server-issued ones could have broken the stateful flow. This
exercises it end to end through HTTP.
"""
import sys

import requests

BASE = "http://127.0.0.1:8000"
ok = True


def step(label, response, expect):
    global ok
    answer = (response.get("answer") or "")
    good = expect.lower() in answer.lower()
    ok = ok and good
    print(("  PASS  " if good else "  FAIL  ") + label)
    print("        " + answer.replace("\n", " ")[:150])
    return response


s = requests.Session()
sid = s.post(BASE + "/api/chat/session", timeout=30).json()["session_id"]
print(f"session: {sid}\n")


def say(msg):
    r = s.post(BASE + "/api/chat", json={"session_id": sid, "message": msg}, timeout=120)
    r.raise_for_status()
    return r.json()


print("=== Multi-turn payment flow ===")
# The rebuilt corpus deliberately refuses to invent a per-kilogram overweight
# tariff, because Kenya Airways does not publish one. A name-correction fee on
# an international booking is a figure the corpus does publish, so it is the
# scenario that legitimately reaches settlement.
step("turn 1 quotes a grounded fee",
     say("I need to fix a typo in my name on my international booking"), "")
step("turn 2 accepts the offer", say("Yes, I would like to pay it now"), "")
d = say("0712345678")
step("turn 3 pushes the prompt to the handset", d, "")

payment = d.get("payment")
print("\n=== Payment record ===")
if payment:
    for k in ("source", "success", "amount", "phone_number", "checkout_request_id"):
        print(f"  {k}: {payment.get(k)}")
else:
    print("  no payment object returned")
    ok = False

print("\n=== State is bound to the session ===")
other = requests.Session()
other.post(BASE + "/api/chat/session", timeout=30)
r = other.post(BASE + "/api/chat",
               json={"session_id": sid, "message": "0712345678"}, timeout=60)
bound = r.status_code == 403
ok = ok and bound
print(("  PASS  " if bound else "  FAIL  ") +
      f"another session cannot continue this payment (got {r.status_code})")

print("\n=== Reset clears pending payment ===")
s.post(BASE + "/api/chat/reset", timeout=30)
new_sid = s.post(BASE + "/api/chat/session", timeout=30).json()["session_id"]
fresh = new_sid != sid
ok = ok and fresh
print(("  PASS  " if fresh else "  FAIL  ") + "reset issued a new session")

print("\n" + "=" * 56)
print("RESULT: " + ("all checks passed" if ok else "FAILURES PRESENT"))
sys.exit(0 if ok else 1)
