"""Privileged, root-private draft store. No live configuration mutations."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess

import secure_profile_catalog as catalog
from secure_profile_catalog import _identifier
from secure_ingress import domain, plan_ingress
from transport_profiles import ProfileError, require_secure_profile, REALITY_PROFILES, _port

STORE = Path("/var/lib/router-orchestrator-secure-profiles")
PKCS8 = bytes.fromhex("302e020100300506032b656e04220420")
SPKI = bytes.fromhex("302a300506032b656e032100")
COMMON = {"name", "transport", "port", "server_name"}
TLS = {"certificate_file", "key_file"}
REALITY = {"server_names", "target_host", "target_port", "private_key", "public_key", "short_ids"}


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_key(value):
    try:
        if not isinstance(value, str) or len(value) != 43:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=")
        if len(raw) != 32 or b64(raw) != value:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise ProfileError("invalid X25519 key encoding") from None


def openssl(args, data=None):
    try:
        result = subprocess.run(["openssl", *args], input=data, capture_output=True,
                                timeout=5, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("X25519 operation unavailable") from None


def reality_keys(profile):
    private = profile.get("private_key")
    public = profile.get("public_key")
    if not private and not public:
        der = openssl(["genpkey", "-algorithm", "X25519", "-outform", "DER"])
        if len(der) != len(PKCS8) + 32 or not der.startswith(PKCS8):
            raise ProfileError("unexpected X25519 private key format")
        private = b64(der[len(PKCS8):])
    elif not private or not public:
        raise ProfileError("provide both REALITY keys or leave both empty")
    der = PKCS8 + decode_key(private)
    result = openssl(["pkey", "-inform", "DER", "-pubout", "-outform", "DER"], der)
    if len(result) != len(SPKI) + 32 or not result.startswith(SPKI):
        raise ProfileError("unexpected X25519 public key format")
    derived = b64(result[len(SPKI):])
    if public and not secrets.compare_digest(decode_key(public), decode_key(derived)):
        raise ProfileError("REALITY key pair does not match")
    profile["private_key"], profile["public_key"] = private, derived
    if not profile.get("short_ids"):
        profile["short_ids"] = [secrets.token_hex(8)]


def normalize_profile(value):
    require_secure_profile(value)
    transport = value["transport"]
    allowed = COMMON | (REALITY if transport in REALITY_PROFILES else TLS)
    if transport in ("wss", "xhttp", "reality-xhttp"):
        allowed |= {"path"}
    if transport in ("grpc", "reality-grpc"):
        allowed |= {"service_name"}
    if set(value) - allowed:
        raise ProfileError("unknown profile field")
    profile = dict(value)
    if transport in REALITY_PROFILES:
        reality_keys(profile)
    return profile


@contextmanager
def connection(root=STORE):
    root = Path(root)
    # The host systemd StateDirectory creates this root-owned private directory.
    # Never create the store below the HUB-writable data directory.
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ProfileError("private profile store directory is not secure")
    path = root / "profiles.db"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ProfileError("private profile database is not secure")
    db = sqlite3.connect(str(path), timeout=5)
    try:
        with db:
            catalog.initialize(db)
            db.execute("""CREATE TABLE IF NOT EXISTS secure_profile_artifacts(
                profile_id TEXT NOT NULL, revision INTEGER NOT NULL,
                artifact BLOB NOT NULL, PRIMARY KEY(profile_id, revision))""")
            yield db
    finally:
        db.close()


def save_draft(db, *, profile, label, remote_host, public_port, occupied_ports=()):
    profile = normalize_profile(profile)
    remote_host = domain(remote_host)
    public_port = _port(public_port)
    plan = plan_ingress(profile, public_port=public_port, occupied_ports=occupied_ports)
    # Plan only: the external nginx inventory has NOT been checked here.
    artifact = json.dumps({"profile": profile, "remote_host": remote_host,
                           "public_port": public_port}, sort_keys=True,
                          separators=(",", ":")).encode()
    digest = hashlib.sha256(artifact).hexdigest()
    revision = catalog.create_draft(db, profile_id=profile["name"], label=label,
                                    transport=profile["transport"], hostname=remote_host,
                                    public_port=public_port, artifact_sha256=digest)
    db.execute("INSERT INTO secure_profile_artifacts VALUES (?,?,?)",
               (profile["name"], revision, artifact))
    return {"profile_id": profile["name"], "revision": revision,
            "transport": profile["transport"], "hostname": remote_host,
            "public_port": public_port, "backend_port": plan["backend_port"],
            "published": False, "applied": False}


def list_revisions(db):
    rows = db.execute("""SELECT profile_id,revision,label,transport,hostname,public_port,published
                          FROM secure_profile_revisions ORDER BY label,revision DESC""")
    fields = ("profile_id","revision","label","transport","hostname","public_port","published")
    return [dict(zip(fields, row)) for row in rows]

def load_artifact(db, profile_id, revision):
    """Return sealed private artifact to privileged caller only."""
    _identifier(profile_id)
    if type(revision) is not int or revision < 1:
        raise ProfileError("invalid revision")
    row = db.execute("""SELECT a.artifact,r.artifact_sha256,r.published
          FROM secure_profile_artifacts a JOIN secure_profile_revisions r
          USING(profile_id,revision) WHERE a.profile_id=? AND a.revision=?""",
          (profile_id, revision)).fetchone()
    if not row:
        raise ProfileError("profile revision not found")
    artifact, digest, published = row
    if not isinstance(artifact, bytes) or not secrets.compare_digest(hashlib.sha256(artifact).hexdigest(), digest):
        raise ProfileError("profile artifact digest mismatch")
    return json.loads(artifact), digest, bool(published)
