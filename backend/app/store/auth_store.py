"""Authentication: login codes, sessions, device trust/fingerprinting, user
identity records, per-user AI settings, and the (still-live, password-based)
alternate credential path used alongside OTP by verify_workbook_access."""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from .presence_store import _touch_daily_activity, create_notification
from .schema import _get_connection, _utcnow


def create_login_code(email: str, code_hash: str, expires_at: str) -> None:
    conn = _get_connection()
    try:
        normalized = email.lower()
        now = datetime.now(timezone.utc)
        recent = conn.execute(
            "SELECT CREATED_AT FROM AUTH_LOGIN_CODES WHERE EMAIL=? ORDER BY ID DESC LIMIT 1",
            (normalized,),
        ).fetchone()
        if recent:
            created = datetime.fromisoformat(recent[0])
            if (now - created).total_seconds() < settings.otp_request_cooldown_seconds:
                raise PermissionError("Please wait before requesting another sign-in code")
        window_start = (now.timestamp() - 3600)
        requests_last_hour = 0
        for row in conn.execute(
            "SELECT CREATED_AT FROM AUTH_LOGIN_CODES WHERE EMAIL=? ORDER BY ID DESC LIMIT 20",
            (normalized,),
        ).fetchall():
            if datetime.fromisoformat(row[0]).timestamp() >= window_start:
                requests_last_hour += 1
        if requests_last_hour >= settings.otp_requests_per_hour:
            raise PermissionError("Too many sign-in codes requested. Try again later")
        conn.execute(
            "UPDATE AUTH_LOGIN_CODES SET USED_AT=? WHERE EMAIL=? AND USED_AT IS NULL",
            (_utcnow(), normalized),
        )
        conn.execute(
            """
            INSERT INTO AUTH_LOGIN_CODES (EMAIL, CODE_HASH, EXPIRES_AT, CREATED_AT)
            VALUES (?, ?, ?, ?)
            """,
            (normalized, code_hash, expires_at, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def consume_login_code(email: str, code_hash: str) -> bool:
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT ID, CODE_HASH, ATTEMPTS, MAX_ATTEMPTS FROM AUTH_LOGIN_CODES
            WHERE EMAIL=? AND USED_AT IS NULL AND EXPIRES_AT>?
            ORDER BY ID DESC LIMIT 1
            """,
            (email.lower(), _utcnow()),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        if not hmac.compare_digest(row["CODE_HASH"], code_hash):
            attempts = int(row["ATTEMPTS"]) + 1
            used_at = _utcnow() if attempts >= int(row["MAX_ATTEMPTS"]) else None
            conn.execute(
                "UPDATE AUTH_LOGIN_CODES SET ATTEMPTS=?, USED_AT=COALESCE(?, USED_AT) WHERE ID=?",
                (attempts, used_at, row["ID"]),
            )
            conn.commit()
            return False
        conn.execute("UPDATE AUTH_LOGIN_CODES SET USED_AT=? WHERE ID=?", (_utcnow(), row[0]))
        conn.commit()
        return True
    finally:
        conn.close()


def create_auth_session(
    session_id: str, user_id: str, token_hash: str, expires_at: str,
    organization_id: str | None = None,
) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO AUTH_SESSIONS
                (SESSION_ID, USER_ID, TOKEN_HASH, CREATED_AT, EXPIRES_AT, ORGANIZATION_ID, LAST_SEEN_AT)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, token_hash, _utcnow(), expires_at, organization_id, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def auth_session_is_active(
    session_id: str, user_id: str, token_hash: str
) -> bool:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT S.TOKEN_HASH FROM AUTH_SESSIONS S
            LEFT JOIN APP_USERS U ON U.USER_ID=S.USER_ID
            WHERE S.SESSION_ID=? AND S.USER_ID=? AND S.REVOKED_AT IS NULL AND S.EXPIRES_AT>?
              AND COALESCE(U.STATUS,'ACTIVE')='ACTIVE'
            """,
            (session_id, user_id, _utcnow()),
        ).fetchone()
        active = bool(row and hmac.compare_digest(row[0], token_hash))
        if active:
            conn.execute("UPDATE AUTH_SESSIONS SET LAST_SEEN_AT=? WHERE SESSION_ID=?", (_utcnow(), session_id))
            conn.commit()
        return active
    finally:
        conn.close()


def revoke_auth_session(session_id: str, user_id: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE AUTH_SESSIONS SET REVOKED_AT=? WHERE SESSION_ID=? AND USER_ID=?",
            (_utcnow(), session_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_device_fingerprint(
    user_id: str, *, session_id: str | None, ip_address: str | None,
    user_agent: str | None, machine_id: str | None,
) -> dict[str, Any] | None:
    """Upsert the (user, machine) device row. Returns None when no
    machine_id is supplied (browsers/clients that haven't sent one yet
    aren't fingerprinted — this is opt-in via the X-Device-Id header, not
    silently defaulted to a per-request throwaway id)."""
    if not machine_id:
        return None
    conn = _get_connection()
    try:
        now = _utcnow()
        fingerprint_id = f"FGP_{uuid.uuid4().hex[:16].upper()}"
        conn.execute(
            """
            INSERT INTO DEVICE_FINGERPRINTS
                (FINGERPRINT_ID, USER_ID, SESSION_ID, IP_ADDRESS, MACHINE_ID, USER_AGENT,
                 TRUST_STATUS, FIRST_SEEN_AT, LAST_SEEN_AT)
            VALUES (?, ?, ?, ?, ?, ?, 'UNKNOWN', ?, ?)
            ON CONFLICT(USER_ID, MACHINE_ID) DO UPDATE SET
                SESSION_ID=excluded.SESSION_ID, IP_ADDRESS=excluded.IP_ADDRESS,
                USER_AGENT=excluded.USER_AGENT, LAST_SEEN_AT=excluded.LAST_SEEN_AT
            """,
            (fingerprint_id, user_id, session_id, ip_address, machine_id, user_agent, now, now),
        )
        _touch_daily_activity(conn, user_id, now)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM DEVICE_FINGERPRINTS WHERE USER_ID=? AND MACHINE_ID=?", (user_id, machine_id)
        ).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def device_trust_status(user_id: str, machine_id: str | None) -> str | None:
    """Returns the device's TRUST_STATUS, or None if unknown/no machine_id
    supplied (callers should treat None as "not gated", not as blocked)."""
    if not machine_id:
        return None
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT TRUST_STATUS FROM DEVICE_FINGERPRINTS WHERE USER_ID=? AND MACHINE_ID=?",
            (user_id, machine_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_device_trust_status(fingerprint_id: str, trust_status: str) -> dict[str, Any]:
    if trust_status not in {"TRUSTED", "UNKNOWN", "BLOCKED"}:
        raise ValueError("trust_status must be TRUSTED, UNKNOWN, or BLOCKED")
    conn = _get_connection()
    try:
        device = conn.execute("SELECT USER_ID, MACHINE_ID FROM DEVICE_FINGERPRINTS WHERE FINGERPRINT_ID=?", (fingerprint_id,)).fetchone()
        if not device:
            raise KeyError("Device fingerprint does not exist")
        conn.execute(
            "UPDATE DEVICE_FINGERPRINTS SET TRUST_STATUS=? WHERE FINGERPRINT_ID=?",
            (trust_status, fingerprint_id),
        )
        if trust_status == "TRUSTED" and device["MACHINE_ID"]:
            # Trusting a device is the explicit, owner-driven way to
            # re-bind that user's mismatched working copies to it (e.g.
            # they got a new laptop) — otherwise a zero-touch device lock
            # would permanently strand a legitimate user on a lost device.
            conn.execute(
                """
                UPDATE WORKING_COPIES SET BOUND_MACHINE_ID=?
                WHERE USER_ID=? AND STATUS='ACTIVE' AND BOUND_MACHINE_ID IS NOT NULL AND BOUND_MACHINE_ID!=?
                """,
                (device["MACHINE_ID"], device["USER_ID"], device["MACHINE_ID"]),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM DEVICE_FINGERPRINTS WHERE FINGERPRINT_ID=?", (fingerprint_id,)).fetchone()
        result = {key.lower(): row[key] for key in row.keys()}
    finally:
        conn.close()
    if trust_status == "BLOCKED":
        try:
            create_notification(
                device["USER_ID"], "DEVICE_BLOCKED", "A device was blocked",
                f"Device {device['MACHINE_ID'] or fingerprint_id} was blocked and can no longer access your workbooks.",
                resource_type="DEVICE", resource_id=fingerprint_id,
            )
        except Exception:
            pass
    return result


def bind_or_verify_device(working_copy_id: str, machine_id: str | None) -> dict[str, Any]:
    """Zero-touch device lock for a working copy: the FIRST device that
    successfully opens it is auto-bound, with no owner action required.
    Every later open attempt from a DIFFERENT device is reported as a
    MISMATCH — even with a byte-identical file path, a valid signature,
    and a valid OTP — because ``machine_id`` is generated into browser
    localStorage (see frontend ``getDeviceId()``), which never travels
    with a copied/emailed .xlsx. This is what actually stops a leaked
    copy of the workbook from loading data on a different machine.
    Trusting a device (``set_device_trust_status(..., 'TRUSTED')``) is
    the explicit, owner-driven way to re-bind a working copy to a new
    device (e.g. the legitimate user got a new laptop)."""
    if not machine_id:
        return {"status": "UNVERIFIED"}
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT BOUND_MACHINE_ID FROM WORKING_COPIES WHERE WORKING_COPY_ID=?", (working_copy_id,)
        ).fetchone()
        if not row:
            raise ValueError("Working copy does not exist")
        bound = row["BOUND_MACHINE_ID"]
        if not bound:
            conn.execute(
                "UPDATE WORKING_COPIES SET BOUND_MACHINE_ID=? WHERE WORKING_COPY_ID=?",
                (machine_id, working_copy_id),
            )
            conn.commit()
            return {"status": "BOUND", "machine_id": machine_id}
        if bound == machine_id:
            return {"status": "MATCH", "machine_id": machine_id}
        return {"status": "MISMATCH", "bound_machine_id": bound, "attempted_machine_id": machine_id}
    finally:
        conn.close()


def list_repository_devices(repository_id: str) -> list[dict[str, Any]]:
    """Every known device belonging to a user with membership on this
    repository (legacy REPOSITORY_MEMBERS — mirrors the membership model
    already used by repository_role()/REPOSITORY_MEMBERS elsewhere).

    LAST_SEEN_AT is scoped to this repository: a device is per-user, not
    per-repository, so its raw DEVICE_FINGERPRINTS.LAST_SEEN_AT reflects
    activity on ANY repository that user touched — showing it here would
    silently leak cross-repository activity into a per-repository view.
    REPOSITORY_DAILY_ACTIVITY tracks per-user, per-repository presence, so
    the most recent entry there is the only thing "last seen" can mean on
    this repository's device list. NULL (never recorded activity on this
    specific repository) is returned as-is rather than falling back to the
    device's global timestamp, so the UI can say "never on this repository"
    instead of showing a number that is quietly about a different repository.

    GLOBAL_LAST_SEEN_AT is the device's own (unscoped) timestamp, kept
    alongside for consumers with a genuinely different, cross-repository
    need — e.g. app.ai.rbac_anomaly's "was this untrusted device active
    anywhere recently" security check, where global recency is the actual
    signal, not an accidental leak."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT D.FINGERPRINT_ID, D.USER_ID, D.SESSION_ID, D.IP_ADDRESS, D.WIFI_SSID,
                   D.MACHINE_ID, D.USER_AGENT, D.TRUST_STATUS, D.FIRST_SEEN_AT,
                   RA.LAST_SEEN_AT AS LAST_SEEN_AT,
                   D.LAST_SEEN_AT AS GLOBAL_LAST_SEEN_AT,
                   U.EMAIL, U.DISPLAY_NAME
            FROM DEVICE_FINGERPRINTS D
            JOIN APP_USERS U ON U.USER_ID = D.USER_ID
            LEFT JOIN (
                SELECT USER_ID, MAX(LAST_SEEN_AT) AS LAST_SEEN_AT
                FROM REPOSITORY_DAILY_ACTIVITY
                WHERE REPOSITORY_ID=?
                GROUP BY USER_ID
            ) RA ON RA.USER_ID = D.USER_ID
            WHERE D.USER_ID IN (
                SELECT USER_ID FROM REPOSITORY_MEMBERS WHERE REPOSITORY_ID=?
                UNION
                SELECT CREATED_BY FROM WORKBOOK_REPOSITORIES WHERE REPOSITORY_ID=?
            )
            ORDER BY LAST_SEEN_AT DESC
            """,
            (repository_id, repository_id, repository_id),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def list_organization_devices(organization_id: str) -> list[dict[str, Any]]:
    """Every known device belonging to any active member of this
    organization, across every repository they belong to — the org-wide
    counterpart to list_repository_devices()."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT D.*, U.EMAIL, U.DISPLAY_NAME
            FROM DEVICE_FINGERPRINTS D
            JOIN APP_USERS U ON U.USER_ID = D.USER_ID
            WHERE D.USER_ID IN (
                SELECT USER_ID FROM ORGANIZATION_MEMBERS WHERE ORGANIZATION_ID=? AND STATUS='ACTIVE'
            )
            ORDER BY D.LAST_SEEN_AT DESC
            """,
            (organization_id,),
        ).fetchall()
        return [{key.lower(): row[key] for key in row.keys()} for row in rows]
    finally:
        conn.close()


def revoke_session(session_id: str, organization_id: str) -> None:
    """Revoke one active auth session, scoped to sessions belonging to a
    member of this organization (prevents cross-org revocation)."""
    conn = _get_connection()
    try:
        session = conn.execute(
            """SELECT S.SESSION_ID FROM AUTH_SESSIONS S
               JOIN ORGANIZATION_MEMBERS M ON M.USER_ID=S.USER_ID AND M.ORGANIZATION_ID=?
               WHERE S.SESSION_ID=? AND S.REVOKED_AT IS NULL""",
            (organization_id, session_id),
        ).fetchone()
        if not session:
            raise KeyError("Session does not exist or is not in this organization")
        conn.execute("UPDATE AUTH_SESSIONS SET REVOKED_AT=? WHERE SESSION_ID=?", (_utcnow(), session_id))
        conn.commit()
    finally:
        conn.close()


def get_or_create_user(email: str) -> dict[str, Any]:
    # Local import: avoids a module-load-time cycle, since dataset_store.py
    # imports get_or_create_user/bind_or_verify_device/etc from this module.
    from .dataset_store import _accept_pending_invitations
    normalized = email.strip().lower()
    conn = _get_connection()
    try:
        now = _utcnow()
        row = conn.execute("SELECT * FROM APP_USERS WHERE EMAIL=?", (normalized,)).fetchone()
        if row:
            conn.execute("UPDATE APP_USERS SET LAST_LOGIN_AT=? WHERE USER_ID=?", (now, row["USER_ID"]))
            _accept_pending_invitations(conn, row["USER_ID"], normalized, now)
            conn.commit()
            return {key.lower(): row[key] for key in row.keys()}

        user_id = f"USR_{uuid.uuid4().hex[:12].upper()}"
        display_name = normalized.split("@", 1)[0].replace(".", " ").title()
        conn.execute(
            """
            INSERT INTO APP_USERS
                (USER_ID, EMAIL, DISPLAY_NAME, ROLE, CREATED_AT, LAST_LOGIN_AT)
            VALUES (?, ?, ?, 'member', ?, ?)
            """,
            (user_id, normalized, display_name, now, now),
        )
        _accept_pending_invitations(conn, user_id, normalized, now)
        conn.commit()
        return {
            "user_id": user_id,
            "email": normalized,
            "display_name": display_name,
            "role": "member",
            "created_at": now,
            "last_login_at": now,
        }
    finally:
        conn.close()


def get_user(user_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute("SELECT * FROM APP_USERS WHERE USER_ID=?", (user_id,)).fetchone()
        return {key.lower(): row[key] for key in row.keys()} if row else None
    finally:
        conn.close()


def save_user_ai_settings(user_id: str, encrypted_key: str, model: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO USER_AI_SETTINGS (USER_ID, API_KEY_ENCRYPTED, MODEL, UPDATED_AT)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(USER_ID) DO UPDATE SET
                API_KEY_ENCRYPTED=excluded.API_KEY_ENCRYPTED,
                MODEL=excluded.MODEL,
                UPDATED_AT=excluded.UPDATED_AT
            """,
            (user_id, encrypted_key, model, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_ai_settings(user_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT API_KEY_ENCRYPTED, MODEL, UPDATED_AT FROM USER_AI_SETTINGS WHERE USER_ID=?",
            (user_id,),
        ).fetchone()
        return {
            "api_key_encrypted": row[0], "model": row[1], "updated_at": row[2]
        } if row else None
    finally:
        conn.close()


def delete_user_ai_settings(user_id: str) -> None:
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM USER_AI_SETTINGS WHERE USER_ID=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def hash_password(password: str, salt: str | None = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"{salt}:{key.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    if not hashed or ":" not in hashed:
        return False
    salt, expected_hex = hashed.split(":", 1)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return hmac.compare_digest(key.hex(), expected_hex)


def set_user_password(user_id_or_email: str, password: str) -> dict[str, Any]:
    conn = _get_connection()
    try:
        normalized = user_id_or_email.strip()
        row = conn.execute(
            "SELECT * FROM APP_USERS WHERE USER_ID=? OR LOWER(EMAIL)=LOWER(?)",
            (normalized, normalized),
        ).fetchone()
        if not row:
            raise ValueError("User not found.")
        hashed = hash_password(password)
        conn.execute("UPDATE APP_USERS SET PASSWORD_HASH=? WHERE USER_ID=?", (hashed, row["USER_ID"]))
        conn.commit()
        return {key.lower(): row[key] for key in row.keys()}
    finally:
        conn.close()


def verify_user_credentials(user_id_or_email: str, password: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        normalized = user_id_or_email.strip()
        row = conn.execute(
            "SELECT * FROM APP_USERS WHERE USER_ID=? OR LOWER(EMAIL)=LOWER(?)",
            (normalized, normalized),
        ).fetchone()
        if not row:
            return None
        pwd_hash = row["PASSWORD_HASH"] if "PASSWORD_HASH" in row.keys() else None
        if not pwd_hash:
            if password:
                new_hash = hash_password(password)
                conn.execute("UPDATE APP_USERS SET PASSWORD_HASH=? WHERE USER_ID=?", (new_hash, row["USER_ID"]))
                conn.commit()
                return {key.lower(): row[key] for key in row.keys()}
            return None
        if not verify_password(password, pwd_hash):
            return None
        return {key.lower(): row[key] for key in row.keys()}
    finally:
        conn.close()


