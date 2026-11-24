"""Exercise the new disk transaction on a disposable tree, never live OpenWrt.

Only platform integration (UCI, Xray identity, boot/procd registration and
sync) is stubbed; the shell's actual backup, hash, lock, mutation, deadline,
worker and recovery functions operate on real fixture files.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/router_client_transaction.sh"
spec = importlib.util.spec_from_file_location("builder", ROOT / "tools/build_router_bundle.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("flock"), "flock required")
class ClientTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ro-disk-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "router"
        self.state = self.fixture / "etc/router-orchestrator/upgrade"
        self.stage = self.state / "stage"
        self.backup = self.state / "backup"
        self.stage.mkdir(parents=True)
        (self.backup / "files").mkdir(parents=True)
        self.originals = {}
        source = self.root / "source"
        inv, hashes = [], []
        for index, (rel, _) in enumerate(builder.FILES):
            candidate = source / "files" / rel
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("# candidate\n")
            if index == 1:
                inv.append(f"0 {rel}\n")
                hashes.append(f"- {rel}\n")
                continue
            live = self.fixture / rel
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("# original " + rel + "\n")
            live.chmod(0o751)
            copy = self.backup / "files" / rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, copy)
            self.originals[rel] = live.read_bytes()
            inv.append(f"1 {rel}\n")
            hashes.append(f"{digest(copy)} {rel}\n")
        (self.backup / "present.txt").write_text("".join(inv))
        (self.backup / "original.sha256").write_text("".join(hashes))
        self.sha = builder.build(self.stage / "candidate.tar", source)
        import tarfile
        with tarfile.open(self.stage / "candidate.tar") as tar:
            tar.extractall(self.stage / "unpacked")
        (self.stage / "archive.sha256").write_text(self.sha)
        (self.stage / "target").write_text("fixture\nws\n443\n")
        for rel, seal in [("etc/config/owrtremote", "uci.sha256"),
                          ("etc/xray/owrt-remote-client.json", "xray.sha256")]:
            p = self.fixture / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("private fixture\n")
            (self.stage / seal).write_text(digest(p) + "\n")
        (self.fixture / "etc/rc.d").mkdir()
        script = SOURCE.read_text()
        script = script.replace('[ "$(id -u)" = 0 ] || fail', ': || fail')
        script = script.replace('[ -r /etc/openwrt_release ] || fail', ': || fail')
        for var in ("S", "INIT", "BOOT", "UCI", "XRAY"):
            script = script.replace(f"\n{var}=/", f"\n{var}=$FIXTURE/")
        script = script.replace('"/$rel"', '"$FIXTURE/$rel"')
        marker = '\ncase "${1:-}" in\narm)'
        self.assertIn(marker, script)
        stubs = """
uci() {
    case "$*" in
        *'changes owrtremote') return 0;;
        *'router_id') echo fixture;;
        *'vps_transport') echo ws;;
        *'vps_port') echo 443;;
        *) return 1;;
    esac
}
xray_identity() { echo '123:456'; }
sync() { :; }
supervised() { ready; }
install_recovery() {
    printf '# fixture\\n' > "$INIT"
    ln -s "$INIT" "$BOOT"
}
"""
        script = script.replace(marker, "\n" + stubs + marker)
        self.script = self.state / "client-transaction.sh"
        self.script.write_text(script)
        self.env = {**os.environ, "FIXTURE": str(self.fixture)}
        self.workers = []
        self.addCleanup(self.stop_workers)

    def call(self, *args, ok=True):
        p = subprocess.run(["sh", str(self.script), *args], env=self.env,
                           capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def stop_workers(self):
        for p in self.workers:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def arm(self):
        self.call("arm", "fixture", self.sha, "180")

    def start_workers(self):
        for role in ("primary", "secondary"):
            p = subprocess.Popen(["sh", str(self.script), "worker", role],
                                 env=self.env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            self.workers.append(p)
        for _ in range(50):
            if "TWO WORKERS READY" in self.call("status").stdout:
                return
            time.sleep(.1)
        self.fail("worker handshake not ready")

    def assert_originals(self):
        for rel, data in self.originals.items():
            self.assertEqual((self.fixture / rel).read_bytes(), data)
            self.assertEqual((self.fixture / rel).stat().st_mode & 0o777, 0o751)
        self.assertFalse((self.fixture / builder.FILES[1][0]).exists())

    def test_apply_requires_two_workers(self):
        self.arm()
        self.call("apply", self.sha, ok=False)
        self.assert_originals()

    def test_apply_then_manual_restore(self):
        self.arm()
        self.start_workers()
        self.call("apply", self.sha)
        for rel, _ in builder.FILES:
            self.assertEqual((self.fixture / rel).read_text(), "# candidate\n")
        self.call("rollback")
        self.assert_originals()
        self.assertIn("ROLLED_BACK", self.call("status").stdout)

    def test_independent_worker_recovers_expired_pending(self):
        self.arm()
        self.start_workers()
        self.call("apply", self.sha)
        (self.state / "client.deadline").write_text("0\n")
        for _ in range(40):
            if not (self.state / "client.pending").exists():
                break
            time.sleep(.2)
        self.assertFalse((self.state / "client.pending").exists())
        self.assert_originals()

    def test_boot_recovers_even_with_no_workers(self):
        self.arm()
        self.start_workers()
        self.call("apply", self.sha)
        self.stop_workers()
        self.call("boot-recover")
        self.assert_originals()

    def test_confirm_retains_candidate(self):
        self.arm()
        self.start_workers()
        self.call("apply", self.sha)
        self.call("confirm", "bad", ok=False)
        self.call("confirm", (self.state / "client.nonce").read_text().strip())
        self.call("boot-recover")
        self.assertIn("CONFIRMED_ON_DISK", self.call("status").stdout)
        for rel, _ in builder.FILES:
            self.assertEqual((self.fixture / rel).read_text(), "# candidate\n")


if __name__ == "__main__":
    unittest.main()
