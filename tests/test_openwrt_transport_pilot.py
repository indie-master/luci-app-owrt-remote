"""Static safety contract for isolated OpenWrt shadow transport pilot."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] /
          "files/usr/sbin/owrt-remote")


class OpenWrtTransportPilotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text()
        cls.start = cls.source.split("pilot_start() (", 1)[1].split("\ntransport_dir=", 1)[0]

    def test_pilot_has_independent_short_ttl(self):
        self.assertIn("sleep 180", self.start)
        self.assertIn("transport-pilot-expire", self.start)
        self.assertIn("nohup", self.start)

    def test_pilot_does_not_reconfigure_live_service(self):
        for command in ("uci set", "uci commit", "/etc/init.d/owrt-remote restart",
                        "procd_set_param", "systemctl", "killall xray"):
            with self.subTest(command=command):
                self.assertNotIn(command, self.start)

    def test_shadow_reverse_tags_cannot_equal_primary(self):
        self.assertIn("pilot would reuse current admin reverse tag", self.start)
        self.assertIn("pilot would reuse current SSH reverse tag", self.start)

    def test_pid_verified_before_termination(self):
        stop = self.source.split("pilot_stop() {", 1)[1].split("\npilot_status() {", 1)[0]
        self.assertIn('if pilot_matches "$pid"; then', stop)
        self.assertIn('kill -TERM "$pid"', stop)

    def test_expiry_checks_exact_pid_start_time_and_config_argument(self):
        code = self.source.split("pilot_matches() {", 1)[1].split("\npilot_status() {", 1)[0]
        # The sandbox hides child /proc entries. Use process metadata and a
        # signal recorder so this regression cannot signal an unrelated PID.
        code = "pilot_matches() {" + code.replace("/proc/$1/", "$pilot_dir/proc/$1/")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = str(root / "client.json")
            for pid, argument in ((100, good), (200, good), (300, good + ".backup")):
                proc = root / "proc" / str(pid)
                proc.mkdir(parents=True)
                (proc / "cmdline").write_bytes(f"xray\0run\0-config\0{argument}\0".encode())

            def expire(current, pid, started):
                (root / "pid").write_text(str(current))
                result = subprocess.run(
                    ["sh", "-c", code + "\n" +
                     'pilot_dir="$1"; shift; '
                     'xray_process_start() { printf "42"; }; '
                     'kill() { printf "%s" "$2"; }; '
                     'pilot_expire "$@"', "pilot-test", directory, str(pid), str(started)],
                    capture_output=True, text=True, check=True)
                return result.stdout

            self.assertEqual(expire(200, 100, 42), "")
            self.assertEqual(expire(200, 200, 41), "")
            self.assertEqual(expire(300, 300, 42), "")
            self.assertEqual(expire(200, 200, 42), "200")
            self.assertEqual((root / "pid").read_text(), "200")

    def test_client_preflight_does_not_change_uci(self):
        code = self.source.split("transport_preflight() (", 1)[1].split("\n# Time-boxed", 1)[0]
        self.assertIn('render_client_config --stdout', code)
        self.assertIn('validate_xray_config "$candidate"', code)
        self.assertNotIn("uci commit", code)

    def test_promotion_keeps_primary_until_probe_and_recovers_previous_file(self):
        # Real shell and on-disk checkpoints, synthetic network/process outcomes.
        command = r'''
source <(sed '/^cmd=/,$d' "$1")
transport_dir="$2"
transport_work="$2"
transport_stage_file="$2/staged.json"
transport_ref() { cat "$1"; }
transport_stop_pilot() { :; }
pilot_start() { echo pilot >>"$transport_dir/trace"; }
send_heartbeat() { echo heartbeat >>"$transport_dir/trace"; }
stop_xray() { echo restart >>"$transport_dir/trace"; }
uci_get() { printf '%s' "$transport_dir/active.json"; }
jsonfilter() { printf 42; }
transport_wait() {
    echo "$1:$2" >>"$transport_dir/trace"
    [ "$1" != probe ] || [ "$3" != fail ]
}
if [ "$3" = recover ]; then
    transport_recover
else
    transport_wait() {
        echo "$1:$2" >>"$transport_dir/trace"
        [ "$1" != probe ] || [ "$MODE" != fail ]
    }
    MODE="$3"
    transport_promote
fi
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "active.json").write_text("working")
            (root / "staged.json").write_text("candidate")
            def run(mode):
                return subprocess.run(["bash", "-c", command, "transport-test", str(SCRIPT),
                                       directory, mode], capture_output=True, timeout=15)
            self.assertNotEqual(run("fail").returncode, 0)
            self.assertEqual((root / "active.json").read_text(), "working")
            self.assertNotIn("restart", (root / "trace").read_text())
            (root / "pending.json").write_text("working")
            (root / "active.json").write_text("candidate")
            self.assertEqual(run("recover").returncode, 0)
            self.assertEqual((root / "active.json").read_text(), "working")
            self.assertFalse((root / "pending.json").exists())
            (root / "trace").unlink()
            self.assertEqual(run("success").returncode, 0)
            self.assertEqual((root / "active.json").read_text(), "candidate")
            self.assertEqual((root / "last-good.json").read_text(), "working")
            self.assertEqual((root / "trace").read_text().splitlines(),
                             ["heartbeat", "pilot", "probe:candidate", "restart", "heartbeat",
                              "activate:candidate"])


if __name__ == "__main__":
    unittest.main()

class TransportLifecycleReliabilityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "transport").mkdir()
        (self.root / "work").mkdir()
        (self.root / "jsonfilter.py").write_text(r"""
import json, re, sys
args = iter(sys.argv[1:])
stream = sys.stdin
for arg in args:
    if arg == "-i":
        stream = open(next(args))
    elif arg == "-e":
        path = next(args)
try:
    value = json.load(stream)
    for part in re.findall(r"\.([\w]+)|\[(\d+)\]", path):
        value = value[part[0]] if part[0] else value[int(part[1])]
except (ValueError, KeyError, IndexError, TypeError):
    sys.exit(1)
print(value if isinstance(value, str) else json.dumps(value))
""")

    def run_agent(self, commands, *args):
        bootstrap = r'''
source <(sed '/^cmd=/,$d' "$1")
root="$2"
shift 2
export TMPDIR="$root"
export OWRT_REMOTE_XRAY_RUNTIME="$root/runtime"
transport_dir="$root/transport"
transport_work="$root/work"
transport_stage_file="$transport_dir/staged.json"
jsonfilter() { "$PYTHON" "$root/jsonfilter.py" "$@"; }
curl() { return 1; }
'''
        return subprocess.run(
            ["bash", "-c", bootstrap + commands, "transport-test", str(SCRIPT), str(self.root), *args],
            env={**os.environ, "PYTHON": sys.executable}, text=True, capture_output=True, timeout=20,
        )

    def test_rotation_preserves_old_credential_and_recovers_lost_confirmation(self):
        old, candidate = "A" * 43, "B" * 43
        fixture = r'''
mode="$1"; candidate="$2"; old="$3"
uci_get() {
    case "$1" in
        hub_url) printf 'https://hub.test' ;;
        router_id) printf 'router' ;;
        transport_identity) printf '%s' "$old" ;;
    esac
}
post_json() {
    [ "$1" = https://hub.test/api/agent/transport-identity ] &&
    [ "$2" = "$candidate" ] &&
    grep -Fq "$candidate" "$transport_dir/identity-pending" || return 1
    case "$mode" in
        offline) return 28 ;;
        reject) printf '{"ok":true,"identity_accepted":false}'; return 0 ;;
    esac
    if [ "$mode" = lost ]; then
        touch "$root/server-committed"
        return 28
    fi
    [ -f "$root/server-committed" ] || return 1
    printf '{"ok":true,"identity_accepted":true}'
}
mv() {
    if [ "$mode" = disk-error ] && [ "${*: -1}" = "$transport_dir/identity" ]; then return 1; fi
    command mv "$@"
}
if [ ! -e "$transport_dir/identity-pending" ]; then
    printf '%s\n' "$candidate" | transport_bind_identity
else
    transport_retry_identity
fi
rc=$?
expected="$old"
[ "$mode" != success ] || expected="$candidate"
[ "$(transport_active_identity)" = "$expected" ] || exit 3
exit "$rc"
'''
        pending = self.root / "transport/identity-pending"
        active = self.root / "transport/identity"
        for mode, rc, saved in [("offline", 1, True), ("reject", 2, False),
                                ("lost", 1, True), ("disk-error", 1, True), ("success", 0, False)]:
            with self.subTest(mode=mode):
                result = self.run_agent(fixture, mode, candidate, old)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertEqual(pending.exists(), saved)
                self.assertNotIn(old, result.stdout + result.stderr)
                self.assertNotIn(candidate, result.stdout + result.stderr)
                if saved:
                    self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertEqual(active.read_text(), "https://hub.test:443 " + candidate + "\n")
        self.assertEqual(active.stat().st_mode & 0o777, 0o600)

    def test_stale_assignment_is_rejected_before_staging(self):
        fixture = r'''
target="$(transport_profile_target live 4 "$(printf 'a%.0s' {1..64})")"
transport_request() { cat "$root/assignments"; }
transport_stage() { touch "$root/staged"; }
transport_execute_task stage 0 "$target"
'''
        target = {"profile_id": "live", "revision": 4, "assignment_id": "a" * 64}
        for stale in (dict(target, profile_id="other"), dict(target, revision=3),
                      dict(target, assignment_id="b" * 64)):
            (self.root / "assignments").write_text(json.dumps({"profiles": [stale, target]}))
            self.assertNotEqual(self.run_agent(fixture).returncode, 0)
            self.assertFalse((self.root / "staged").exists())
        (self.root / "assignments").write_text(json.dumps({"profiles": [target]}))
        self.assertEqual(self.run_agent(fixture).returncode, 0)
        self.assertTrue((self.root / "staged").exists())

    def test_result_recovery_after_sigkill_and_lost_ack_does_not_reapply(self):
        fixture = r'''
mode="$1"
transport_request() {
    if [ "$#" = 1 ]; then
        printf '%s\n' '{"ok":true,"task":{"task_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","action":"stage","slot":"enrollment","target":null}}'
        return
    fi
    [ "$2:$3:$4" = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:null:true ] || return 1
    printf 'ack\n' >>"$root/trace"
    [ "$mode" != lost-ack ]
}
transport_stop_pilot() { :; }
transport_recover() { :; }
transport_ref() { cat "$1"; }
transport_execute_task() {
    printf 'apply\n' >>"$root/trace"
    printf '%s' "$3" >"$transport_stage_file"
    kill -KILL "$BASHPID"
}
transport_run_task
'''
        journal = self.root / "transport/task-journal"
        self.assertEqual(self.run_agent(fixture, "kill").returncode, -signal.SIGKILL)
        self.assertTrue(journal.read_text().endswith(" started\n"))
        self.assertEqual(self.run_agent(fixture, "lost-ack").returncode, 1)
        self.assertTrue(journal.read_text().endswith(" true\n"))
        self.assertEqual(self.run_agent(fixture, "retry").returncode, 0)
        self.assertFalse(journal.exists())
        self.assertEqual((self.root / "trace").read_text().splitlines(), ["apply", "ack", "ack"])

    def test_interrupted_activation_commits_verified_running_target_without_restart(self):
        fixture = r'''
target="$(transport_profile_target live 4 "$(printf 'a%.0s' {1..64})")"
printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb activate 0 %s started\n' "$target" >"$transport_dir/task-journal"
printf '%s' "$target" >"$transport_dir/active.json"
printf null >"$transport_dir/pending.json"
transport_stop_pilot() { :; }
transport_ref() { cat "$1"; }
uci_get() { printf '%s' "$transport_dir/active.json"; }
xray_running() { :; }
send_heartbeat() { :; }
stop_xray() { touch "$root/unexpected-restart"; return 1; }
transport_wait() {
    [ "$1:$2" = "activate:$target" ] || return 1
    printf 'verify\n' >>"$root/trace"
}
transport_request() {
    [ "$2:$3:$4" = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:$target:true" ] || return 1
    printf 'ack\n' >>"$root/trace"
}
transport_execute_task() { touch "$root/unexpected-restart"; return 1; }
transport_run_task
'''
        result = self.run_agent(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "unexpected-restart").exists())
        self.assertFalse((self.root / "transport/pending.json").exists())
        self.assertEqual((self.root / "transport/last-good.json").read_text(), "null")
        self.assertEqual((self.root / "trace").read_text().splitlines(), ["verify", "ack"])

    def test_transport_manager_serializes_task_execution(self):
        fixture = r'''
transport_run_task() {
    mkdir "$root/in-flight" || return 1
    printf 'start\n' >>"$root/trace"
    sleep 1
    printf 'finish\n' >>"$root/trace"
    rmdir "$root/in-flight"
}
transport_manager run-task & first=$!
for attempt in {1..50}; do
    [ ! -d "$root/in-flight" ] || break
    sleep 0.02
done
[ -d "$root/in-flight" ] || exit 2
transport_manager run-task & second=$!
wait "$first" && wait "$second"
'''
        result = self.run_agent(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "trace").read_text().splitlines(),
                         ["start", "finish", "start", "finish"])
