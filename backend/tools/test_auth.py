"""Verifies the admin authentication and passenger session-ownership fixes.

Each check asserts the *server* enforces the rule. Hiding a control in the
browser is not a security boundary, so every assertion here is made against
the HTTP API directly.
"""
import sys

import requests

BASE = "http://127.0.0.1:8000"
PASSWORD = "Frankaren123."

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (" :: " + detail if detail else ""))


print("\n=== 1. Admin routes reject anonymous callers ===")
for path in ("/api/admin/overview", "/api/admin/transactions",
             "/api/admin/model-comparison", "/api/admin/audit"):
    r = requests.get(BASE + path, timeout=30)
    check(f"{path} -> 401", r.status_code == 401, f"got {r.status_code}")

print("\n=== 2. Session probe is safe to leave open ===")
r = requests.get(BASE + "/api/admin/session", timeout=10)
body = r.json()
check("unauthenticated probe returns 200", r.status_code == 200)
check("probe reports not authenticated", body.get("authenticated") is False)
check("probe leaks no data", set(body.keys()) == {"authenticated"}, str(body))

print("\n=== 3. Wrong credentials are rejected ===")
r = requests.post(BASE + "/api/admin/login",
                  json={"username": "admin", "password": "wrong-password"}, timeout=30)
check("bad password -> 401", r.status_code == 401, f"got {r.status_code}")
bad_user = requests.post(BASE + "/api/admin/login",
                         json={"username": "nosuchuser", "password": PASSWORD}, timeout=30)
check("unknown username -> 401", bad_user.status_code == 401)
check("failure message does not reveal which field was wrong",
      r.json().get("detail") == bad_user.json().get("detail"),
      f"{r.json().get('detail')!r} vs {bad_user.json().get('detail')!r}")

print("\n=== 4. Correct credentials grant access ===")
s = requests.Session()
r = s.post(BASE + "/api/admin/login",
           json={"username": "admin", "password": PASSWORD}, timeout=30)
check("login -> 200", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
check("session cookie issued", "kq_staff" in s.cookies)

cookie = next((c for c in s.cookies if c.name == "kq_staff"), None)
if cookie:
    check("cookie is HttpOnly", "HttpOnly" in str(cookie._rest).replace("httponly", "HttpOnly"),
          str(cookie._rest))
    check("cookie token is not the password", PASSWORD not in cookie.value)

for path in ("/api/admin/overview", "/api/admin/transactions", "/api/admin/audit"):
    r = s.get(BASE + path, timeout=60)
    check(f"authenticated {path} -> 200", r.status_code == 200, f"got {r.status_code}")

print("\n=== 5. Audit trail records the access ===")
entries = s.get(BASE + "/api/admin/audit", timeout=30).json().get("entries", [])
actions = {e["action"] for e in entries}
check("login_success recorded", "login_success" in actions, str(sorted(actions)))
check("login_failed recorded", "login_failed" in actions)
check("data reads recorded", "view_overview" in actions or "view_transactions" in actions)

print("\n=== 6. Logout revokes the session server-side ===")
s.post(BASE + "/api/admin/logout", timeout=30)
r = s.get(BASE + "/api/admin/overview", timeout=30)
check("after logout -> 401", r.status_code == 401, f"got {r.status_code}")

print("\n=== 7. A stolen/forged cookie does not work ===")
forged = requests.Session()
forged.cookies.set("kq_staff", "forged-token-value", domain="127.0.0.1")
r = forged.get(BASE + "/api/admin/overview", timeout=30)
check("forged cookie -> 401", r.status_code == 401, f"got {r.status_code}")

print("\n=== 8. Passenger sessions are server-issued ===")
p = requests.Session()
r = p.post(BASE + "/api/chat/session", timeout=30)
sid = r.json()["session_id"]
check("session issued", r.status_code == 200 and sid.startswith("cs_"), sid)
check("identifier is unguessable (>=24 chars)", len(sid) >= 24, f"len={len(sid)}")
check("session cookie set", "kq_session" in p.cookies)

r2 = p.post(BASE + "/api/chat/session", timeout=30)
check("reload resumes the same session", r2.json()["session_id"] == sid)

print("\n=== 9. One passenger cannot address another's session ===")
victim = requests.Session()
victim_id = victim.post(BASE + "/api/chat/session", timeout=30).json()["session_id"]
attacker = requests.Session()
attacker.post(BASE + "/api/chat/session", timeout=30)
r = attacker.post(BASE + "/api/chat",
                  json={"session_id": victim_id, "message": "What is the baggage allowance?"},
                  timeout=120)
check("supplying another session_id -> 403", r.status_code == 403, f"got {r.status_code}")

print("\n=== 10. Input bounds are enforced ===")
r = p.post(BASE + "/api/chat", json={"session_id": sid, "message": "x" * 100000}, timeout=30)
check("100,000-character message -> 422", r.status_code == 422, f"got {r.status_code}")
r = p.post(BASE + "/api/chat", json={"session_id": sid, "message": "   "}, timeout=30)
check("whitespace-only message rejected", r.status_code == 422, f"got {r.status_code}")

print("\n=== 11. Normal chat still works ===")
r = p.post(BASE + "/api/chat",
           json={"session_id": sid, "message": "What is the checked baggage allowance?"},
           timeout=120)
check("chat -> 200", r.status_code == 200, f"got {r.status_code}")
if r.status_code == 200:
    d = r.json()
    check("answer returned", bool(d.get("answer")), str(d.get("answer"))[:90])
    check("session echoed back", d.get("session_id") == sid)

print("\n=== 12. Login lockout after repeated failures ===")
# A throwaway username is used deliberately: the throttle key is username+IP,
# so hammering the real account here would lock the operator out of their own
# dashboard for the whole lockout window, and any later test - or a live
# demonstration - would then fail for the wrong reason.
locked = requests.Session()
codes = []
for i in range(7):
    rr = locked.post(BASE + "/api/admin/login",
                     json={"username": "bruteforce-probe", "password": "guess%d" % i},
                     timeout=30)
    codes.append(rr.status_code)
check("brute force eventually throttled", 429 in codes, str(codes))
check("probing a different account does not lock the real one",
      requests.post(BASE + "/api/admin/login",
                    json={"username": "admin", "password": PASSWORD},
                    timeout=30).status_code == 200)

print("\n" + "=" * 60)
print(f"PASSED {len(passed)}   FAILED {len(failed)}")
if failed:
    for f in failed:
        print("  - " + f)
sys.exit(1 if failed else 0)
