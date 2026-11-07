"""SQLite persistence layer for Errand Agent.

Manages:
- Providers & catalog (seeded deterministically)
- Holds (10-minute temporary reservations with quoted price)
- Bookings & Idempotency ledger (prevents duplicate bookings across retries/restarts)
- Action journal (pre-execution log so a process crash mid-booking can be recovered)
- Approvals (code-level cryptographic/exact-match binding to action, provider, slot, price)
- Persistent Memories (explicit vs inferred, active vs forgotten, confirmed status)
- Traces (structured observability log per run/turn)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "errand_agent.db"


@dataclass
class ApprovalRecord:
    approval_id: str
    user_id: str
    action: str  # "book" | "cancel" | "reschedule"
    provider_id: Optional[str]
    slot: Optional[str]
    price: Optional[int]
    booking_id: Optional[str]
    fingerprint: str
    granted_at: float
    consumed: bool = False
    invalidated: bool = False
    invalidation_reason: Optional[str] = None


def compute_approval_fingerprint(
    action: str,
    provider_id: Optional[str] = None,
    slot: Optional[str] = None,
    price: Optional[int] = None,
    booking_id: Optional[str] = None,
) -> str:
    """Compute canonical hash binding an approval to exact material parameters."""
    payload = json.dumps(
        {
            "action": (action or "").strip().lower(),
            "provider_id": (provider_id or "").strip(),
            "slot": (slot or "").strip(),
            "price": int(price) if price is not None else None,
            "booking_id": (booking_id or "").strip(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_idempotency_key(
    user_id: str,
    action: str,
    target_id: str,
    slot: str = "",
) -> str:
    """Deterministic idempotency key so retries/restarts reuse the exact same key."""
    raw = f"{user_id.strip()}|{action.strip().lower()}|{target_id.strip()}|{slot.strip()}"
    return "idem_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ErrandStore:
    """SQLite store backing tools, approvals, idempotency, memory, and traces."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, clock=time.time):
        self.db_path = str(db_path)
        self._clock = clock
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def now(self) -> float:
        return float(self._clock())

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS holds (
                hold_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                service TEXT NOT NULL,
                slot TEXT NOT NULL,
                search_price INTEGER NOT NULL,
                quoted_price INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' -- active | booked | expired
            );

            CREATE TABLE IF NOT EXISTS bookings (
                booking_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                service TEXT NOT NULL,
                area TEXT NOT NULL,
                slot TEXT NOT NULL,
                price INTEGER NOT NULL,
                hold_id TEXT,
                idempotency_key TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'confirmed', -- confirmed | cancelled | provider_cancelled | rescheduled
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS idempotency_ledger (
                idempotency_key TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target_id TEXT NOT NULL,
                booking_id TEXT,
                outcome_status TEXT NOT NULL, -- success | slot_taken | hold_expired | cancelled | rescheduled | slot_conflict
                response_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cancel_attempts (
                booking_id TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS action_journal (
                journal_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                hold_id TEXT,
                booking_id TEXT,
                slot TEXT,
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL, -- started | completed | resolved_after_timeout
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                provider_id TEXT,
                slot TEXT,
                price INTEGER,
                booking_id TEXT,
                fingerprint TEXT NOT NULL,
                granted_at REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                invalidated INTEGER NOT NULL DEFAULT 0,
                invalidation_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'explicit', -- explicit | inferred
                confirmed INTEGER NOT NULL DEFAULT 1,    -- 1 for explicit or user-confirmed inferred, 0 for unconfirmed inferred
                status TEXT NOT NULL DEFAULT 'active',   -- active | forgotten
                tags TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_messages (
                msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    # ── Approvals (Application-Enforced Gate) ─────────────────────────────────
    def grant_approval(
        self,
        user_id: str,
        action: str,
        provider_id: Optional[str] = None,
        slot: Optional[str] = None,
        price: Optional[int] = None,
        booking_id: Optional[str] = None,
    ) -> ApprovalRecord:
        """Record an explicit user approval for exact material details.

        Invalidates any prior unconsumed approvals for the same user and action
        so stale approvals can never be replayed.
        """
        self._conn.execute(
            """
            UPDATE approvals
               SET invalidated = 1,
                   invalidation_reason = 'superseded_by_newer_approval'
             WHERE user_id = ? AND action = ? AND consumed = 0 AND invalidated = 0
            """,
            (user_id, action.lower()),
        )
        fp = compute_approval_fingerprint(action, provider_id, slot, price, booking_id)
        rec = ApprovalRecord(
            approval_id="appr_" + uuid.uuid4().hex[:12],
            user_id=user_id,
            action=action.lower(),
            provider_id=provider_id,
            slot=slot,
            price=int(price) if price is not None else None,
            booking_id=booking_id,
            fingerprint=fp,
            granted_at=self.now(),
        )
        self._conn.execute(
            """
            INSERT INTO approvals (
                approval_id, user_id, action, provider_id, slot, price,
                booking_id, fingerprint, granted_at, consumed, invalidated, invalidation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL)
            """,
            (
                rec.approval_id,
                rec.user_id,
                rec.action,
                rec.provider_id,
                rec.slot,
                rec.price,
                rec.booking_id,
                rec.fingerprint,
                rec.granted_at,
            ),
        )
        self._conn.commit()
        return rec

    def invalidate_approvals(
        self,
        user_id: str,
        reason: str,
        action: Optional[str] = None,
    ) -> int:
        """Invalidate active approvals when provider, slot, or quoted price changes."""
        if action:
            cur = self._conn.execute(
                """
                UPDATE approvals
                   SET invalidated = 1, invalidation_reason = ?
                 WHERE user_id = ? AND action = ? AND consumed = 0 AND invalidated = 0
                """,
                (reason, user_id, action.lower()),
            )
        else:
            cur = self._conn.execute(
                """
                UPDATE approvals
                   SET invalidated = 1, invalidation_reason = ?
                 WHERE user_id = ? AND consumed = 0 AND invalidated = 0
                """,
                (reason, user_id),
            )
        self._conn.commit()
        return cur.rowcount

    def verify_and_consume_approval(
        self,
        user_id: str,
        action: str,
        provider_id: Optional[str] = None,
        slot: Optional[str] = None,
        price: Optional[int] = None,
        booking_id: Optional[str] = None,
    ) -> ApprovalRecord:
        """Verify that an active, unconsumed approval exists matching the exact parameters.

        Raises PermissionError if missing, invalidated, or mismatched.
        Note: If a retry happens with the same parameters after a transient failure
        (like 503 on cancel or network timeout on book), we allow re-verifying a
        consumed approval ONLY if its fingerprint matches and it was not invalidated.
        """
        expected_fp = compute_approval_fingerprint(action, provider_id, slot, price, booking_id)
        row = self._conn.execute(
            """
            SELECT * FROM approvals
             WHERE user_id = ? AND action = ? AND invalidated = 0
             ORDER BY granted_at DESC, rowid DESC LIMIT 1
            """,
            (user_id, action.lower()),
        ).fetchone()

        if not row:
            raise PermissionError(
                f"APPROVAL_REQUIRED: No active approval found for action='{action}'."
            )
        if row["fingerprint"] != expected_fp:
            # Material change detected! Invalidate the stale approval immediately.
            self.invalidate_approvals(
                user_id,
                reason=(
                    f"material_change_detected: approved=({row['provider_id']},{row['slot']},{row['price']},{row['booking_id']}) "
                    f"vs requested=({provider_id},{slot},{price},{booking_id})"
                ),
                action=action,
            )
            raise PermissionError(
                f"APPROVAL_MISMATCH: Material details changed since approval "
                f"(approved provider={row['provider_id']}, slot={row['slot']}, price={row['price']}, booking={row['booking_id']}; "
                f"requested provider={provider_id}, slot={slot}, price={price}, booking={booking_id})."
            )

        self._conn.execute(
            "UPDATE approvals SET consumed = 1 WHERE approval_id = ?",
            (row["approval_id"],),
        )
        self._conn.commit()
        return ApprovalRecord(
            approval_id=row["approval_id"],
            user_id=row["user_id"],
            action=row["action"],
            provider_id=row["provider_id"],
            slot=row["slot"],
            price=row["price"],
            booking_id=row["booking_id"],
            fingerprint=row["fingerprint"],
            granted_at=row["granted_at"],
            consumed=True,
            invalidated=bool(row["invalidated"]),
            invalidation_reason=row["invalidation_reason"],
        )

    # ── Action Journal (Crash / Restart Recovery) ────────────────────────────
    def log_action_start(
        self,
        user_id: str,
        action: str,
        idempotency_key: str,
        hold_id: Optional[str] = None,
        booking_id: Optional[str] = None,
        slot: Optional[str] = None,
    ) -> str:
        existing = self._conn.execute(
            "SELECT journal_id FROM action_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return existing["journal_id"]
        jid = "jrnl_" + uuid.uuid4().hex[:12]
        now = self.now()
        self._conn.execute(
            """
            INSERT INTO action_journal (
                journal_id, user_id, action, hold_id, booking_id, slot,
                idempotency_key, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)
            """,
            (jid, user_id, action, hold_id, booking_id, slot, idempotency_key, now, now),
        )
        self._conn.commit()
        return jid

    def log_action_complete(self, idempotency_key: str, state: str = "completed") -> None:
        self._conn.execute(
            "UPDATE action_journal SET state = ?, updated_at = ? WHERE idempotency_key = ?",
            (state, self.now(), idempotency_key),
        )
        self._conn.commit()

    def get_pending_actions(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM action_journal WHERE user_id = ? AND state = 'started' ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Persistent Memory Management ─────────────────────────────────────────
    def set_memory(
        self,
        user_id: str,
        key: str,
        value: str,
        source: str = "explicit",
        confirmed: Optional[bool] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Insert or update a memory. Explicit memories are confirmed by default;
        inferred memories default to unconfirmed (confirmed=0) until approved."""
        assert source in ("explicit", "inferred"), f"Invalid memory source: {source}"
        if confirmed is None:
            is_confirmed = 1 if source == "explicit" else 0
        else:
            is_confirmed = 1 if confirmed else 0
        now = self.now()
        tags_json = json.dumps(tags or [])

        existing = self._conn.execute(
            "SELECT memory_id FROM memories WHERE user_id = ? AND key = ? AND status = 'active'",
            (user_id, key),
        ).fetchone()

        if existing:
            mid = existing["memory_id"]
            self._conn.execute(
                """
                UPDATE memories
                   SET value = ?, source = ?, confirmed = ?, tags = ?, updated_at = ?
                 WHERE memory_id = ?
                """,
                (value, source, is_confirmed, tags_json, now, mid),
            )
        else:
            mid = "mem_" + uuid.uuid4().hex[:10]
            self._conn.execute(
                """
                INSERT INTO memories (
                    memory_id, user_id, key, value, source, confirmed, status, tags, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (mid, user_id, key, value, source, is_confirmed, tags_json, now, now),
            )
        self._conn.commit()
        return self.get_memory(mid)  # type: ignore

    def get_memory(self, memory_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["confirmed"] = bool(d["confirmed"])
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    def forget_memory(self, user_id: str, key: str) -> int:
        """Mark a memory key as forgotten so it is never injected into model context."""
        cur = self._conn.execute(
            "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE user_id = ? AND key = ? AND status = 'active'",
            (self.now(), user_id, key),
        )
        self._conn.commit()
        return cur.rowcount

    def confirm_inferred_memory(self, user_id: str, key: str) -> int:
        cur = self._conn.execute(
            "UPDATE memories SET confirmed = 1, updated_at = ? WHERE user_id = ? AND key = ? AND status = 'active'",
            (self.now(), user_id, key),
        )
        self._conn.commit()
        return cur.rowcount

    def list_memories(
        self,
        user_id: str,
        include_forgotten: bool = False,
        only_confirmed: bool = False,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM memories WHERE user_id = ?"
        params: list[Any] = [user_id]
        if not include_forgotten:
            q += " AND status = 'active'"
        if only_confirmed:
            q += " AND confirmed = 1"
        q += " ORDER BY updated_at DESC"
        rows = self._conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["confirmed"] = bool(d["confirmed"])
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out

    # ── Conversation & Trace Storage ─────────────────────────────────────────
    def add_message(self, user_id: str, session_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO conversation_messages (user_id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, role, content, self.now()),
        )
        self._conn.commit()

    def get_messages(self, user_id: str, session_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM conversation_messages WHERE user_id = ? AND session_id = ? ORDER BY msg_id ASC LIMIT ?",
                (user_id, session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM conversation_messages WHERE user_id = ? ORDER BY msg_id ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_trace(
        self,
        session_id: str,
        user_id: str,
        turn_index: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        tid = "trc_" + uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO traces (trace_id, session_id, user_id, turn_index, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, session_id, user_id, turn_index, event_type, json.dumps(payload), self.now()),
        )
        self._conn.commit()
        return tid

    def get_traces(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out
