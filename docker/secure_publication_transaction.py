"""Durable two-file publication transaction engine.

Only a privileged, serialized worker may call this engine. Paths are supplied
by the worker's reviewed deployment contract, not by a browser request.
Service validation, activation and authenticated acceptance are mandatory
callbacks. There is no default production backend and no CLI entry point.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from transport_profiles import ProfileError


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProfileError("publication requires regular single-link files")
        return stream.read(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def atomic(path, data, mode=0o600, owner=None):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".ro-publication-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


class PublicationTransaction:
    def __init__(self, root, xray_path, nginx_path):
        self.root = Path(root)
        self.paths = {"xray": Path(xray_path), "nginx": Path(nginx_path)}
        meta = self.root.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ProfileError("unsafe publication transaction directory")
        if self.paths["xray"].resolve() == self.paths["nginx"].resolve():
            raise ProfileError("publication targets must be distinct")
        self.journal = self.root / "pending.json"

    @contextmanager
    def locked(self):
        fd = os.open(self.root / "transaction.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ProfileError("unsafe publication lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _write_journal(self, record):
        atomic(self.journal, json.dumps(record, sort_keys=True).encode())

    def _record(self):
        data, _, _, _ = read_regular(self.journal)
        return json.loads(data)

    def _verified_backup(self, record, name):
        entry = record["files"][name]
        data, _, _, _ = read_regular(self.root / (name + ".backup"))
        if sha(data) != entry["before"]:
            raise ProfileError("publication backup digest mismatch")
        current = read_regular(self.paths[name])[0]
        allowed = {entry["before"], entry["after"]}
        if "trial" in entry:
            allowed.add(entry["trial"])
        if sha(current) not in allowed:
            raise ProfileError("publication target changed externally; manual recovery required")
        return data, entry

    def _restore(self, record, activate, verify_baseline):
        # Verify both backups and both targets before overwriting either.
        backups = {name: self._verified_backup(record, name) for name in self.paths}
        record["phase"] = "RESTORING"
        self._write_journal(record)
        for name, (data, entry) in backups.items():
            atomic(self.paths[name], data, entry["mode"], (entry["uid"], entry["gid"]))
        activate()
        if not verify_baseline():
            raise ProfileError("rollback files restored but baseline runtime not verified")
        record["phase"] = "ROLLED_BACK"
        self._write_journal(record)

    def _finalize(self, record, finalize):
        if finalize is None:
            raise ProfileError("publication metadata finalizer required")
        for name, path in self.paths.items():
            if sha(read_regular(path)[0]) != record["files"][name]["after"]:
                raise ProfileError("accepted publication files changed; manual recovery required")
        finalize(record)
        record["phase"] = "ACCEPTED"
        self._write_journal(record)
        return record

    def apply(self, *, candidate_xray, candidate_nginx, expected_xray_sha,
              expected_nginx_sha, validate, activate, accept, verify_baseline,
              binding=None, finalize=None, trial_xray=None, trial_accept=None):
        """Accept only after explicit runtime evidence; optional metadata finalizer.

        The caller must hold the common Xray lifecycle lock, prevent unrelated
        nginx edits, and install an external recovery service before invoking.
        A crash leaves a durable journal for that recovery service. FILES_ACCEPTED
        is a forward-only commit decision; the finalizer must be idempotent and
        independently check runtime and metadata before exposing the revision.
        """
        with self.locked():
            if self.journal.exists() or self.journal.is_symlink():
                raise ProfileError("transaction journal already exists; archive or recover it")
            if (trial_xray is None) != (trial_accept is None):
                raise ProfileError("trial configuration and acceptance must be supplied together")
            if trial_xray is not None and (not isinstance(trial_xray, bytes) or trial_xray == candidate_xray):
                raise ProfileError("invalid distinct trial configuration")
            candidates = {"xray": candidate_xray, "nginx": candidate_nginx}
            expected = {"xray": expected_xray_sha, "nginx": expected_nginx_sha}
            originals = {name: read_regular(path) for name, path in self.paths.items()}
            if (binding is None) != (finalize is None):
                raise ProfileError("binding and metadata finalizer must be supplied together")
            record = {"phase": "PREPARED", "files": {}}
            if binding is not None:
                record["binding"] = binding
            for name, (data, mode, uid, gid) in originals.items():
                if sha(data) != expected[name]:
                    raise ProfileError("publication baseline changed")
                if not isinstance(candidates[name], bytes):
                    raise ProfileError("invalid publication candidate")
                record["files"][name] = {"before": sha(data), "after": sha(candidates[name]),
                                         "mode": mode, "uid": uid, "gid": gid}
            # Preflight may not mutate live targets. Recheck immediately after.
            validate(candidate_xray, candidate_nginx)
            if trial_xray is not None:
                record["files"]["xray"]["trial"] = sha(trial_xray)
                validate(trial_xray, candidate_nginx)
            for name, path in self.paths.items():
                if sha(read_regular(path)[0]) != expected[name]:
                    raise ProfileError("publication baseline changed during validation")
            for name, (data, _, _, _) in originals.items():
                atomic(self.root / (name + ".backup"), data)
            self._write_journal(record)
            try:
                record["phase"] = "APPLYING"
                self._write_journal(record)
                for name, path in self.paths.items():
                    entry = record["files"][name]
                    if sha(read_regular(path)[0]) != entry["before"]:
                        raise ProfileError("publication target changed before replacement")
                    data = trial_xray if name == "xray" and trial_xray is not None else candidates[name]
                    atomic(path, data, entry["mode"], (entry["uid"], entry["gid"]))
                record["phase"] = "VERIFYING"
                self._write_journal(record)
                activate()
                if trial_xray is not None:
                    if not trial_accept():
                        raise ProfileError("authenticated trial acceptance failed")
                    entry = record["files"]["xray"]
                    if sha(read_regular(self.paths["xray"])[0]) != entry["trial"] or sha(read_regular(self.paths["nginx"])[0]) != record["files"]["nginx"]["after"]:
                        raise ProfileError("publication files changed during authenticated trial")
                    record["phase"] = "REMOVING_TRIAL"
                    self._write_journal(record)
                    atomic(self.paths["xray"], candidate_xray, entry["mode"], (entry["uid"], entry["gid"]))
                    activate()
                if not accept():
                    raise ProfileError("publication runtime acceptance failed")
                for name, path in self.paths.items():
                    if sha(read_regular(path)[0]) != record["files"][name]["after"]:
                        raise ProfileError("publication target changed during acceptance")
                record["phase"] = "FILES_ACCEPTED" if binding is not None else "ACCEPTED"
                self._write_journal(record)
            except Exception:
                # No metadata finalizer has run before the durable decision.
                # A failed journal fsync must not roll back an already-recorded
                # decision that a recovery worker might subsequently finalize.
                if self._record().get("phase") == "FILES_ACCEPTED":
                    raise
                self._restore(record, activate, verify_baseline)
                raise
            if binding is not None:
                # Forward-only commit: never restore old files after a catalog
                # revision might have become visible to other processes.
                return self._finalize(record, finalize)
            return record

    def recover(self, *, activate, verify_baseline, finalize=None):
        with self.locked():
            record = self._record()
            if record["phase"] in ("ACCEPTED", "ROLLED_BACK"):
                return record
            if record["phase"] == "FILES_ACCEPTED":
                return self._finalize(record, finalize)
            if record["phase"] not in ("PREPARED", "APPLYING", "VERIFYING", "REMOVING_TRIAL", "RESTORING"):
                raise ProfileError("unknown publication journal phase")
            self._restore(record, activate, verify_baseline)
            return record
