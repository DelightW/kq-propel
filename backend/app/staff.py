"""
Individual staff accounts with roles (Priority 5).

Before this, the dashboard had one shared login, so the audit log could only
ever say "admin did it". Each member of staff now signs in as themselves, and
what they can see follows their role (least privilege):

  admin    everything, including staff accounts, erasure and the access log
  agent    customer service: handovers, name corrections, payments, receipts
  analyst  quality metrics and the model comparison; no passenger records

The account configured with ADMIN_USERNAME / ADMIN_PASSWORD(_HASH) is the first
administrator. It is written to the table at every start-up, so the server's
environment always holds a way back in if every other admin is locked out.

Passwords are stored as scrypt hashes (app.auth). A new account gets a one-time
password that is shown once to the admin who created it; the holder must
change it at first sign-in.
"""
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from app import auth, config, database

ADMIN, AGENT, ANALYST = "admin", "agent", "analyst"
ROLES = config.STAFF_ROLES

ROLE_LABELS = {
    ADMIN: "Administrator",
    AGENT: "Customer service agent",
    ANALYST: "Analyst",
}

# What each role's dashboard shows. The server enforces the same rules on every
# route (auth.require_role); this list only decides which panels to draw.
PANELS = {
    ADMIN: ["metrics", "requests", "handovers", "conversations", "transactions", "comparison",
            "knowledge", "audit", "privacy", "staff"],
    AGENT: ["metrics", "requests", "handovers", "conversations", "transactions", "knowledge"],
    ANALYST: ["metrics", "comparison", "knowledge"],
}

# A real hash of a random string, verified against when the username does not
# exist, so a failed sign-in takes the same time whichever half was wrong.
_DUMMY_HASH = auth.hash_password(secrets.token_urlsafe(16))


class StaffError(ValueError):
    pass


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _public(row) -> Dict:
    if row is None:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d["active"] = bool(d.get("active"))
    d["must_change_password"] = bool(d.get("must_change_password"))
    d["role_label"] = ROLE_LABELS.get(d.get("role"), d.get("role"))
    return d


_bootstrapped = False


def _ensure_ready() -> None:
    # Start-up does this too; doing it on first use as well means a process
    # that skipped start-up (a test client, a script) still has its admin.
    global _bootstrapped
    if not _bootstrapped:
        ensure_bootstrap_admin()


def get(username: str) -> Optional[Dict]:
    _ensure_ready()
    with database._connect() as conn:
        row = conn.execute("SELECT * FROM staff_users WHERE username = ? COLLATE NOCASE",
                           ((username or "").strip(),)).fetchone()
    return dict(row) if row else None


def public(username: str) -> Optional[Dict]:
    return _public(get(username))


def list_staff() -> List[Dict]:
    _ensure_ready()
    with database._connect() as conn:
        rows = conn.execute("SELECT * FROM staff_users ORDER BY active DESC, role, username").fetchall()
    return [_public(r) for r in rows]


def ensure_bootstrap_admin() -> None:
    """Writes the environment's credential as an active administrator."""
    global _bootstrapped
    _bootstrapped = True
    cred = auth.credential()
    with database._connect() as conn:
        existing = conn.execute("SELECT username FROM staff_users WHERE username = ? COLLATE NOCASE",
                                (cred.username,)).fetchone()
        if existing:
            conn.execute("UPDATE staff_users SET password_hash = ?, role = ?, active = 1, "
                         "must_change_password = 0 WHERE username = ? COLLATE NOCASE",
                         (cred.password_hash, ADMIN, cred.username))
        else:
            conn.execute("INSERT INTO staff_users (username, display_name, role, password_hash, active, "
                         "must_change_password, created_at, created_by) VALUES (?,?,?,?,1,0,?,?)",
                         (cred.username, "Administrator", ADMIN, cred.password_hash, _now(),
                          "environment"))


def authenticate(username: str, password: str) -> Optional[Dict]:
    """The account when the password is right and the account is active."""
    row = get(username)
    ok = auth.verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok or not row["active"]:
        return None
    with database._connect() as conn:
        conn.execute("UPDATE staff_users SET last_login_at = ? WHERE username = ?",
                     (_now(), row["username"]))
    return _public(row)


def _check_password(password: str) -> None:
    if len(password or "") < config.STAFF_MIN_PASSWORD_LENGTH:
        raise StaffError(f"Passwords must be at least {config.STAFF_MIN_PASSWORD_LENGTH} characters.")


def _check_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise StaffError("Role must be one of: " + ", ".join(ROLES) + ".")
    return role


def _active_admins(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM staff_users WHERE role = ? AND active = 1",
                        (ADMIN,)).fetchone()[0]


def _new_password() -> str:
    return secrets.token_urlsafe(9)  # 12 characters


def create(username: str, display_name: str, role: str, created_by: str) -> Dict:
    """Creates an account and returns it with its one-time password."""
    username = (username or "").strip()
    if not username or len(username) > 40 or not all(c.isalnum() or c in "._-" for c in username):
        raise StaffError("Usernames use letters, digits, '.', '_' or '-' (up to 40).")
    role = _check_role(role)
    if get(username):
        raise StaffError("That username is already taken.")
    password = _new_password()
    with database._connect() as conn:
        conn.execute("INSERT INTO staff_users (username, display_name, role, password_hash, active, "
                     "must_change_password, created_at, created_by) VALUES (?,?,?,?,1,1,?,?)",
                     (username, (display_name or "").strip()[:80] or username, role,
                      auth.hash_password(password), _now(), created_by))
    return {**public(username), "one_time_password": password}


def _guard_last_admin(conn, row: Dict, *, new_role: Optional[str] = None, deactivate: bool = False) -> None:
    losing_admin = row["role"] == ADMIN and row["active"] and (
        deactivate or (new_role is not None and new_role != ADMIN))
    if losing_admin and _active_admins(conn) <= 1:
        raise StaffError("There must always be at least one active administrator.")


def set_role(username: str, role: str) -> Dict:
    role = _check_role(role)
    row = get(username)
    if not row:
        raise LookupError(username)
    with database._connect() as conn:
        _guard_last_admin(conn, row, new_role=role)
        conn.execute("UPDATE staff_users SET role = ? WHERE username = ?", (role, row["username"]))
    return public(row["username"])


def set_active(username: str, active: bool) -> Dict:
    row = get(username)
    if not row:
        raise LookupError(username)
    with database._connect() as conn:
        if not active:
            _guard_last_admin(conn, row, deactivate=True)
        conn.execute("UPDATE staff_users SET active = ? WHERE username = ?",
                     (1 if active else 0, row["username"]))
        if not active:
            # Signs them out everywhere at once, not when their session expires.
            conn.execute("DELETE FROM admin_sessions WHERE username = ? COLLATE NOCASE",
                         (row["username"],))
    return public(row["username"])


def reset_password(username: str) -> Dict:
    row = get(username)
    if not row:
        raise LookupError(username)
    password = _new_password()
    with database._connect() as conn:
        conn.execute("UPDATE staff_users SET password_hash = ?, must_change_password = 1 WHERE username = ?",
                     (auth.hash_password(password), row["username"]))
        conn.execute("DELETE FROM admin_sessions WHERE username = ? COLLATE NOCASE", (row["username"],))
    return {**public(row["username"]), "one_time_password": password}


def change_own_password(username: str, current: str, new: str) -> Dict:
    row = get(username)
    if row and row.get("created_by") == "environment":
        raise StaffError("This account's password is set in the server's environment "
                         "(ADMIN_PASSWORD_HASH); change it there.")
    if not row or not auth.verify_password(current or "", row["password_hash"]):
        raise StaffError("The current password is not correct.")
    _check_password(new)
    if new == current:
        raise StaffError("Choose a password different from the current one.")
    with database._connect() as conn:
        conn.execute("UPDATE staff_users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
                     (auth.hash_password(new), row["username"]))
    return public(row["username"])
