"""Metadata-only profile catalog and per-router selection.

The caller owns authentication and the SQLite transaction. Secrets and actual
Xray configs must live in privileged storage, not in these tables. Publishing
a catalog revision is deliberately distinct from changing router runtime.
No network, nginx, UCI, container or agent calls are made here.
"""
from __future__ import annotations

import json
import re
import sqlite3
from transport_profiles import ProfileError, SECURE_PROFILES
from secure_ingress import domain

IDENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
DIGEST = re.compile(r"[a-f0-9]{64}")


def _identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ProfileError("invalid catalog identifier")
    return value


def initialize(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS secure_profile_revisions(
        profile_id TEXT NOT NULL, revision INTEGER NOT NULL,
        label TEXT NOT NULL, transport TEXT NOT NULL, hostname TEXT NOT NULL,
        public_port INTEGER NOT NULL, artifact_sha256 TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        PRIMARY KEY(profile_id, revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS secure_router_preferences(
        router_id TEXT PRIMARY KEY, desired_profiles TEXT NOT NULL)""")


def create_draft(db, *, profile_id, label, transport, hostname, public_port, artifact_sha256):
    _identifier(profile_id)
    if transport not in SECURE_PROFILES:
        raise ProfileError("catalog only accepts secure profiles")
    if not isinstance(label, str) or not label.strip() or len(label) > 100:
        raise ProfileError("invalid label")
    hostname = domain(hostname)
    if type(public_port) is not int or not 1 <= public_port <= 65535:
        raise ProfileError("invalid public port")
    if not isinstance(artifact_sha256, str) or not DIGEST.fullmatch(artifact_sha256):
        raise ProfileError("invalid artifact digest")
    # One INSERT computes the next immutable revision within the write statement.
    cursor = db.execute("""INSERT INTO secure_profile_revisions
        (profile_id,revision,label,transport,hostname,public_port,artifact_sha256)
        SELECT ?,COALESCE(MAX(revision),0)+1,?,?,?,?,?
        FROM secure_profile_revisions WHERE profile_id=? RETURNING revision""",
        (profile_id,label,transport,hostname,public_port,artifact_sha256,profile_id))
    return cursor.fetchone()[0]


def publish_verified(db, profile_id, revision, *, verified_artifact_sha256):
    """Caller MUST first verify server listener, certificate and ingress live.
    Matching digest binds that caller's evidence to an immutable revision.
    """
    _identifier(profile_id)
    if type(revision) is not int or revision < 1:
        raise ProfileError("invalid revision")
    cursor = db.execute("""UPDATE secure_profile_revisions SET published=1
        WHERE profile_id=? AND revision=? AND artifact_sha256=? AND published=0""",
        (profile_id, revision, verified_artifact_sha256))
    if cursor.rowcount != 1:
        raise ProfileError("draft or verification digest mismatch")


def available_profiles(db):
    rows = db.execute("""SELECT profile_id,revision,label,transport,hostname,public_port
        FROM secure_profile_revisions WHERE published=1
        ORDER BY label,revision DESC""").fetchall()
    fields = ("profile_id","revision","label","transport","hostname","public_port")
    return [dict(zip(fields, row)) for row in rows]


def profile_selection(profiles):
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 3:
        raise ProfileError("select a primary and at most two fallback profiles")
    seen = set()
    for item in profiles:
        if not isinstance(item, dict) or set(item) != {"profile_id","revision"}:
            raise ProfileError("invalid profile selection")
        key = (_identifier(item["profile_id"]), item["revision"])
        if type(key[1]) is not int or not 1 <= key[1] <= 2**63 - 1 or key in seen:
            raise ProfileError("invalid or duplicate revision")
        seen.add(key)
    return seen


def set_desired_profiles(db, router_id, profiles):
    """Save explicit desired order for exactly one router; does NOT activate it."""
    if not isinstance(router_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", router_id):
        raise ProfileError("invalid router identifier")
    for key in profile_selection(profiles):
        if not db.execute("""SELECT 1 FROM secure_profile_revisions
                WHERE profile_id=? AND revision=? AND published=1""", key).fetchone():
            raise ProfileError("profile is not published")
    db.execute("""INSERT INTO secure_router_preferences(router_id,desired_profiles)
        VALUES (?,?) ON CONFLICT(router_id) DO UPDATE SET desired_profiles=excluded.desired_profiles""",
        (router_id,json.dumps(profiles,separators=(",",":"))))


def desired_profiles(db, router_id):
    if not isinstance(router_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", router_id):
        raise ProfileError("invalid router identifier")
    row = db.execute("SELECT desired_profiles FROM secure_router_preferences WHERE router_id=?",
                     (router_id,)).fetchone()
    return json.loads(row[0]) if row else []
