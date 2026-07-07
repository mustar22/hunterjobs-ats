"""
core/companies.py

Company intel cache (companies table): research + contacts once per company,
reused across jobs and scans. Helpers take an open conn.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

_LEGAL_RE = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|plc|llp|pty|ag)\b\.?",
    re.I,
)


def company_key(company: str, domain: str = "") -> str:
    """Stable cache key: bare domain when known (best identity), else the
    cleaned lowercase name. '' when neither is usable."""
    d = (domain or "").strip().lower()
    if d:
        return d
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", (company or ""))
    s = re.split(r"\s[-–—]\s|,", s)[0]
    s = _LEGAL_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s.replace(" ", "-")


def get_cached(conn: sqlite3.Connection, key: str,
               ttl_days: int = 30) -> dict | None:
    """Fresh cache row as a dict (stack/flags/contacts JSON-decoded), else None.
    ttl_days <= 0 = never expires."""
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM companies WHERE company_key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    if ttl_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
        if (row["researched_at"] or "") < cutoff:
            return None
    d = dict(row)
    for f in ("real_stack", "culture_flags", "contacts", "sources"):
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    return d


def save(conn: sqlite3.Connection, key: str, data: dict) -> None:
    """Upsert one enrichment result. Lists are JSON-encoded here."""
    if not key:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO companies (
            company_key, name, domain, yc_slug,
            company_summary, hiring_signal, real_stack, culture_flags,
            company_size, contacts, sources, hunted, researched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            data.get("name") or "",
            data.get("domain") or "",
            data.get("yc_slug") or "",
            data.get("company_summary") or "",
            data.get("hiring_signal") or "uncertain",
            json.dumps(data.get("real_stack") or []),
            json.dumps(data.get("culture_flags") or []),
            data.get("company_size") or "",
            json.dumps(data.get("contacts") or []),
            json.dumps(data.get("sources") or []),
            1 if data.get("hunted") else 0,
            data.get("researched_at") or datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
