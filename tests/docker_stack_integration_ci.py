"""Real unified-stack checks restricted to disposable GitHub-hosted runners."""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import io
import json
import os
import re
from pathlib import Path
import signal
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
import manager as m
from stack_runtime import Ingress, Stack, docker, websocket
from secure_profile_store import connection, load_artifact, reality_keys, save_draft
from secure_reverse_probe import build_probe
from rehearse_secure_reverse import Proof, proof_matches, reserve
from transport_profiles import ProfileError
from orchestrator_stack_config import STREAM_MODULE, render_external, render_internal, stack_plan
from unified_core_manager import PINNED_CORE, baseline as core_baseline, preparation_directory
from xray_releases import VERSION as CORE_VERSION
from unified_xray_adapter import Adapter, XRAY_TMPFS, socket_address
from unified_xray_state import XrayState, encode
from unified_transport_catalog import initialize_stage, record_stage, stage_status

HUB = "hub.example.invalid"
PASSTHROUGH = "pass.example.invalid"
LIFECYCLE_HOST = "lifecycle.example.invalid"
PROFILE_TRANSPORTS = ("wss", "reality-tcp", "xhttp", "grpc")
WS_PATH = "/owrt-ws/"
CI_LABEL = "org.router-orchestrator.integration"


def diagnose_gate(gate):
    diagnostics = sys.stderr

    @wraps(gate)
    def checked(stack, release):
        try:
            return gate(stack, release)
        except Exception as error:
            print(f"CI stack gate failed: {gate.__name__}, mode={stack.plan['mode']}",
                  file=diagnostics, flush=True)
            if isinstance(error, m.Blocked):
                reason = str(error)
                if reason in {"xray configuration test failed", "nginx configuration test failed"}:
                    print(reason, file=diagnostics)
                elif reason.startswith("Readiness failed: "):
                    failed = reason.removeprefix("Readiness failed: ").split(", ")
                    if set(failed) <= {"hub", "xray", "nginx", "HTTPS routing"}:
                        print("Failed readiness checks:", ", ".join(failed), file=diagnostics)
            command_error = error if isinstance(error, subprocess.CalledProcessError) else error.__cause__
            if isinstance(command_error, subprocess.CalledProcessError):
                print("Gate command exit status:", command_error.returncode, file=diagnostics)
            raise
    return checked


def cli(path, *args, expected=0, expected_error=None):
    output = io.StringIO()
    errors = io.StringIO()
    with patch.object(sys, "argv", ["manager.py", "stack", "--env-file", str(path), *map(str, args)]):
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = m.main()
    assert code == expected, (args[0], code, expected)
    if expected_error is not None:
        assert errors.getvalue().strip() == expected_error
    return output.getvalue()


def choose_ports(count):
    with contextlib.ExitStack() as sockets:
        listeners = [sockets.enter_context(socket.socket()) for _ in range(count)]
        for sock in listeners:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in listeners]


def cert(directory, host):
    directory.mkdir()
    directory.chmod(0o755)
    m.call("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
           "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
           "-keyout", str(directory / "privkey.pem"), "-out", str(directory / "fullchain.pem"))
    os.chown(directory / "privkey.pem", 0, m.HUB_GID)
    (directory / "privkey.pem").chmod(0o640)
    (directory / "fullchain.pem").chmod(0o644)


def write_env(path, config):
    m.durable_write(path, "".join(f"{key}={value}\n" for key, value in config.items()))


def fixture(root, mode, project, hub_image, nginx, xray, backend="external_readonly"):
    ws_path = "/ci-unified-wss/" if backend == "unified_adapter" else WS_PATH
    hub, ws, stream, tls, health, public, http = choose_ports(7)
    cert(root / "tls", HUB)
    cert(root / "tls" / "passthrough", PASSTHROUGH)
    if backend == "unified_adapter":
        cert(root / "tls" / LIFECYCLE_HOST, LIFECYCLE_HOST)
        if mode == "standalone":
            for transport in PROFILE_TRANSPORTS[1:]:
                host = f"{transport}.{LIFECYCLE_HOST}"
                cert(root / "tls" / host, host)
    (root / "xray").mkdir()
    (root / "assets").mkdir()

    settings = {"clients": [{"id": str(uuid.uuid4())}], "decryption": "none"}
    loopback = {"listen": "127.0.0.1", "protocol": "vless", "settings": settings}
    xray_tls = "/etc/router-orchestrator/tls/passthrough/"
    xray_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            dict(loopback, port=ws, streamSettings={"network": "ws", "wsSettings": {"path": ws_path}}),
            dict(loopback, port=stream, streamSettings={
                "network": "raw", "security": "tls",
                "tlsSettings": {"certificates": [{"certificateFile": xray_tls + "fullchain.pem",
                                                  "keyFile": xray_tls + "privkey.pem"}]}}),
        ],
        "outbounds": [{"protocol": "blackhole"}],
    }
    xray_file = root / "xray" / "owrt-remote.json"
    xray_file.write_text(json.dumps(xray_config))
    os.chown(xray_file, 0, m.HUB_GID)
    xray_file.chmod(0o640)

    config = {
        "RO_STACK_NAME": project, "RO_STACK_MODE": mode, "RO_PORT": str(hub),
        "RO_WSS_PORT": str(ws), "RO_WSS_PATH": ws_path, "RO_IMAGE": hub_image,
        "RO_STATE_DIR": str(root / "data"), "RO_PUBLIC_URL": f"https://{HUB}:{public}",
        "RO_XRAY_BACKEND": backend, "RO_XRAY_IMAGE": xray,
        "RO_XRAY_CONFIG_DIR": str(root / "xray"), "RO_XRAY_ASSET_DIR": str(root / "assets"),
        "RO_TLS_DIR": str(root / "tls"), "RO_NGINX_IMAGE": nginx,
        "RO_NGINX_CONF": str(root / "nginx.conf"), "RO_HTTP_PORT": str(http),
        "RO_TLS_PORT": str(tls), "RO_NGINX_HEALTH_PORT": str(health),
        "RO_STREAM_ROUTES": f"{PASSTHROUGH}:{stream}",
    }
    nginx_flags = docker("image", "inspect", "--format", "{{.Id}}", nginx).strip()
    result = m.call("docker", "run", "--rm", "--pull=never", "--network=none",
                    nginx_flags, "nginx", "-V")
    assert "--with-stream_ssl_preread_module" in result.stderr
    module = STREAM_MODULE if "--with-stream=dynamic" in result.stderr else ""
    if mode == "standalone":
        config["RO_NGINX_STREAM_MODULE"] = module
    plan = stack_plan(config)
    internal = render_internal(plan)
    if internal:
        (root / "nginx.conf").write_text(internal)
    return config, plan, module


def external_fixture(root, config, plan, module, project, profile_port=None):
    paths = render_external(plan)
    prefix = f"load_module {module};\n" if module else ""
    prefix += "events {}\npid /var/run/nginx.pid;\n"
    rules = paths["external-stream.snippet"]
    if profile_port is not None:
        rules += f"    {LIFECYCLE_HOST} 127.0.0.1:{profile_port};\n"
    http = ""
    if plan["mode"] == "external-only":
        rules += f"    {HUB} 127.0.0.1:{plan['tls'] or config['RO_TLS_PORT']};\n"
        http = (
            "http {\n"
            f"  server {{ listen 127.0.0.1:{config['RO_TLS_PORT']} ssl default_server; "
            "ssl_reject_handshake on; }\n"
            f"  server {{ listen 127.0.0.1:{config['RO_TLS_PORT']} ssl; server_name {HUB};\n"
            "    ssl_certificate /etc/router-orchestrator/tls/fullchain.pem;\n"
            "    ssl_certificate_key /etc/router-orchestrator/tls/privkey.pem;\n"
            + paths["external-http.snippet"] + "  }\n}\n"
        )
    stream = (
        "stream {\n  map $ssl_preread_server_name $backend {\n"
        '    default "";\n' + rules + "  }\n"
        f"  server {{ listen 127.0.0.1:{plan['public']}; ssl_preread on; proxy_pass $backend; }}\n"
        "}\n"
    )
    conf = root / "fixture-nginx.conf"
    conf.write_text(prefix + stream + http)
    mounts = ("--mount", f"type=bind,src={conf},dst=/etc/nginx/nginx.conf,readonly",
              "--mount", f"type=bind,src={root / 'tls'},dst=/etc/router-orchestrator/tls,readonly")
    image = config["RO_NGINX_IMAGE"]
    common = ("--pull=never", "--network=host", "--read-only",
              "--cap-drop", "ALL", "--cap-add", "SETGID", "--cap-add", "SETUID",
              "--cap-add", "CHOWN", "--security-opt", "no-new-privileges:true",
              "--tmpfs", "/var/cache/nginx", "--tmpfs", "/var/run", *mounts)
    docker("run", "--rm", *common, image, "nginx", "-t")
    docker("run", "--detach", "--label", f"{CI_LABEL}={project}", "--name",
           project + "-fixture-ingress", *common, image)


def verify_stack_backup(root, env, config):
    stack = Stack(env, config)
    catalog = stack.transport_store
    catalog.mkdir(mode=0o700, exist_ok=True)
    with connection(catalog) as db:
        draft = save_draft(db, profile={"name": "backup-ci", "transport": "reality-tcp",
                                       "server_name": "backup.example.invalid",
                                       "server_names": ["backup.example.invalid"],
                                       "target_host": "backup.example.invalid",
                                       "target_port": 443, "port": choose_ports(1)[0]},
                           label="Synthetic backup", remote_host="backup.example.invalid",
                           public_port=stack.plan["public"])
        ref = {key: draft[key] for key in ("profile_id", "revision")}
        initialize_stage(db)
        receipt = record_stage(db, "transport.stage", ref, "a" * 64)
        artifact = load_artifact(db, **ref)
    cli(env, "backup", expected=2, expected_error="BLOCKED: Stop the stack before consistency backup")
    cli(env, "stop")
    output = cli(env, "backup")
    archive = next(line.removeprefix("BACKUP: ") for line in output.splitlines()
                   if line.startswith("BACKUP: "))
    destination = root / "restored-bundle"
    with contextlib.redirect_stdout(io.StringIO()):
        m.restore(argparse.Namespace(archive=archive, destination=destination, stack_bundle=True))
    private = destination / "private"
    assert private.stat().st_uid == 0 and private.stat().st_mode & 0o777 == 0o700
    with m.sqlite_read(private / "profiles.db", immutable=True) as db:
        restored = load_artifact(db, **ref)
        assert restored == artifact, "private revision differs after restore"
        assert stage_status(db, **ref) == receipt, "receipt differs after restore"
    with m.sqlite_read(destination / "state/hub.db", immutable=True) as db:
        assert db.execute("SELECT value FROM ro_stack_ci").fetchall() == [("synthetic",)]
    state = XrayState(stack)
    assert (private / "xray-active.json").read_bytes() == state.config.read_bytes()
    if state.exists():
        assert (private / "xray.json").read_bytes() == state.path.read_bytes()
    if stack.plan["nginx_conf"] is not None:
        assert (private / "nginx-active.conf").read_bytes() == stack.plan["nginx_conf"].read_bytes()
    cli(env, "start")
    marker(stack.name)


def fixture_tls_client(document):
    """Use the runner's mounted test CA with Xray's system trust opt-in."""
    result = json.loads(json.dumps(document))
    for outbound in result["outbounds"]:
        stream = outbound.get("streamSettings", {})
        if stream.get("security") == "tls":
            tls = stream["tlsSettings"]
            assert tls.get("allowInsecure", False) is False
            tls["useSystemCA"] = True
    return result


def render_fixture_agent(root, profile, probe, version, proofs):
    """Run the agent's renderer against synthetic UCI on the runner."""
    outbound, ssh = [item for item in probe["outbounds"] if item["protocol"] == "vless"]
    values = dict(os.environ, TMPDIR=str(root), RO_AGENT_SCRIPT=str(m.ROOT / "files/usr/sbin/owrt-remote"),
                  RO_AGENT_VERSION=version, RO_AGENT_LUCI=str(proofs[0].port),
                  RO_AGENT_SSH=str(proofs[1].port),
                  OWRT_REMOTE_CANDIDATE_TRANSPORT={"wss": "ws"}.get(profile["transport"], profile["transport"]),
                  OWRT_REMOTE_CANDIDATE_HOST=outbound["settings"]["address"],
                  OWRT_REMOTE_CANDIDATE_PORT=str(outbound["settings"]["port"]),
                  OWRT_REMOTE_CANDIDATE_UUID=outbound["settings"]["id"],
                  OWRT_REMOTE_CANDIDATE_SSH_UUID=ssh["settings"]["id"],
                  OWRT_REMOTE_CANDIDATE_REVERSE_TAG=outbound["settings"]["reverse"]["tag"],
                  OWRT_REMOTE_CANDIDATE_SSH_REVERSE_TAG=ssh["settings"]["reverse"]["tag"],
                  OWRT_REMOTE_CANDIDATE_SNI=profile["server_name"],
                  OWRT_REMOTE_CANDIDATE_WS_PATH=profile.get("path", ""),
                  OWRT_REMOTE_CANDIDATE_XHTTP_PATH=profile.get("path", ""),
                  OWRT_REMOTE_CANDIDATE_GRPC_SERVICE=profile.get("service_name", ""),
                  OWRT_REMOTE_CANDIDATE_REALITY_PUBLIC_KEY=profile.get("public_key", ""),
                  OWRT_REMOTE_CANDIDATE_REALITY_SHORT_ID=(profile.get("short_ids") or [""])[0])
    runner = """source <(sed '/^cmd=/,$d' "$RO_AGENT_SCRIPT")
uci_get() {
    case "$1" in
        admin_port) printf '%s' "$RO_AGENT_LUCI" ;;
        ssh_port) printf '%s' "$RO_AGENT_SSH" ;;
        *) printf '%s' "$2" ;;
    esac
}
xray_reverse_finalrules_required() {
    [[ "$RO_AGENT_VERSION" == "Xray 26.10.21" ]]
}
render_client_config --stdout
"""
    proc = subprocess.run(["bash", "-c", runner], env=values, capture_output=True, check=True, timeout=15)
    return fixture_tls_client(json.loads(proc.stdout))


def verify_secure_reverse(root, config, module, project):
    """Exercise real transport I/O with synthetic clients on loopback."""
    version = "Xray " + os.environ["RO_CI_XRAY_VERSION"]
    for transport in PROFILE_TRANSPORTS:
        with adapter_check("secure-reverse", transport), contextlib.ExitStack() as resources:
            directory = root / ("reverse-" + transport)
            directory.mkdir(mode=0o700)
            host = "reverse.example.invalid"
            cert(directory / "tls", host)
            (directory / "tls").chmod(0o755)
            (directory / "tls/fullchain.pem").chmod(0o644)
            sockets = [resources.enter_context(reserve()) for _ in range(7)]
            backend, ingress, admin, ssh, _, _, decoy = (sock.getsockname()[1] for sock in sockets)
            proofs = [Proof(sock) for sock in sockets[4:6]]
            for proof in proofs:
                resources.callback(proof.close)
            profile = {"name": "ci-probe", "transport": transport, "port": backend,
                       "server_name": host, "path": "/ci-reverse/", "service_name": "ci_reverse"}
            if transport == "reality-tcp":
                profile.update(server_names=[host], target_host="127.0.0.1", target_port=decoy)
                reality_keys(profile)
                sockets[6].close()
                target = subprocess.Popen(
                    ["openssl", "s_server", "-accept", f"127.0.0.1:{decoy}", "-tls1_3",
                     "-alpn", "h2", "-cert", str(directory / "tls/fullchain.pem"),
                     "-key", str(directory / "tls/privkey.pem"), "-quiet"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                def stop_target():
                    target.terminate()
                    target.wait(timeout=5)
                resources.callback(stop_target)
                deadline = time.monotonic() + 5
                while True:
                    assert target.poll() is None, "REALITY TLS target exited"
                    try:
                        with socket.create_connection(("127.0.0.1", decoy), timeout=0.2):
                            break
                    except OSError:
                        assert time.monotonic() < deadline, "REALITY TLS target unavailable"
                        time.sleep(0.1)
            else:
                profile.update(certificate_file="/etc/router-orchestrator/tls/fullchain.pem",
                               key_file="/etc/router-orchestrator/tls/privkey.pem")

            server, client = build_probe(profile, ingress_port=ingress, admin_port=admin,
                                         ssh_port=ssh, admin_target=proofs[0].port,
                                         ssh_target=proofs[1].port, core_version=version,
                                         proxy_protocol=False)
            invalid = json.loads(json.dumps(client))
            for outbound in invalid["outbounds"]:
                if outbound["protocol"] == "vless":
                    outbound["settings"]["id"] = str(uuid.uuid4())
            fixtures = (("server", server),
                        ("client", render_fixture_agent(directory, profile, client, version, proofs)),
                        ("invalid", render_fixture_agent(directory, profile, invalid, version, proofs)))
            for name, document in fixtures:
                path = directory / (name + ".json")
                path.write_text(json.dumps(document))
                os.chown(path, 0, m.HUB_GID)
                path.chmod(0o640)

            nginx_config = directory / "nginx.conf"
            prefix = f"load_module {module};\n" if module else ""
            nginx_config.write_text(prefix + "events {}\npid /var/run/nginx.pid;\n"
                "stream { map $ssl_preread_server_name $backend { default \"\"; "
                f"{host} 127.0.0.1:{backend}; }} "
                f"server {{ listen 127.0.0.1:{ingress}; ssl_preread on; proxy_pass $backend; }} }}")
            nginx_config.chmod(0o644)

            def start(role, image, options, *command):
                name = f"{project}-{transport}-{role}"
                docker("run", "--detach", "--pull=never", "--name", name,
                       "--label", f"{CI_LABEL}={project}", "--network=host", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                       "--pids-limit=64", "--memory=192m", *options, image, *command)
                resources.callback(docker, "rm", "-f", name)
                return name

            def start_core(role):
                mounts = ("--mount", f"type=bind,src={directory / (role + '.json')},dst=/probe.json,readonly",
                          "--mount", f"type=bind,src={directory / 'tls'},dst=/etc/router-orchestrator/tls,readonly")
                return start(role, config["RO_XRAY_IMAGE"],
                             ("--user", f"{m.HUB_UID}:{m.HUB_GID}",
                              "--env", "SSL_CERT_FILE=/etc/router-orchestrator/tls/fullchain.pem",
                              *(arg for path, options in XRAY_TMPFS.items()
                                for arg in ("--tmpfs", path + ":" + options)), *mounts),
                             "run", "-config", "/probe.json")

            for sock in sockets[:4]:
                sock.close()
            nginx = start("ingress", config["RO_NGINX_IMAGE"],
                          ("--user", f"{m.HUB_UID}:{m.HUB_GID}",
                           "--entrypoint", "nginx", "--tmpfs", "/var/cache/nginx:mode=1777",
                           "--tmpfs", "/var/run:mode=1777",
                           "--mount", f"type=bind,src={nginx_config},dst=/etc/nginx/nginx.conf,readonly"),
                          "-g", "daemon off;")
            server_name = start_core("server")
            invalid_name = start_core("invalid")

            def running(*names):
                assert docker("inspect", "--format", "{{.State.Status}}", *names).split() == [
                    "running"] * len(names), "secure reverse fixture exited"

            deadline = time.monotonic() + 10
            while True:
                running(nginx, server_name, invalid_name)
                try:
                    for port in (backend, ingress, admin, ssh):
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            pass
                    break
                except OSError:
                    assert time.monotonic() < deadline, "reverse listeners did not open"
                    time.sleep(0.2)

            for _ in range(5):
                running(nginx, server_name, invalid_name)
                assert not any(proof_matches(port, proof)
                               for port, proof in zip((admin, ssh), proofs)), "wrong UUID accepted"
                time.sleep(1)

            client_name = start_core("client")
            deadline = time.monotonic() + 40
            while True:
                running(nginx, server_name, client_name)
                if all(proof_matches(port, proof) for port, proof in zip((admin, ssh), proofs)):
                    break
                assert time.monotonic() < deadline, "missing authenticated admin or SSH reverse stream"
                time.sleep(0.5)
            print("Authenticated dual reverse through isolated ingress:", transport, flush=True)


def verify_ingress(root, plan):
    hub_tls = ssl.create_default_context(cafile=str(root / "tls" / "fullchain.pem"))
    other_tls = ssl.create_default_context(cafile=str(root / "tls" / "passthrough" / "fullchain.pem"))
    for attempt in range(25):
        try:
            with contextlib.closing(Ingress(HUB, plan["public"], hub_tls)) as conn:
                conn.request("GET", "/health")
                with conn.getresponse() as response:
                    assert response.status == 200 and json.load(response)["ok"] is True
                conn.request("GET", "/api/auth/meta")
                with conn.getresponse() as response:
                    assert response.status == 200 and json.load(response)["ok"] is True
            assert websocket(Ingress(HUB, plan["public"], hub_tls), HUB, plan["wss_path"])
            with contextlib.closing(Ingress(PASSTHROUGH, plan["public"], other_tls)) as conn:
                conn.connect()
            break
        except OSError:
            if attempt == 24:
                raise
            time.sleep(1)
    try:
        with contextlib.closing(Ingress("unknown.example.invalid", plan["public"], hub_tls)) as conn:
            conn.connect()
    except (OSError, ssl.SSLError):
        pass
    else:
        raise AssertionError("Unknown SNI was accepted")


def marker(project, create=False):
    program = (
        "import sqlite3\n"
        "with sqlite3.connect('/var/lib/owrt-remote/hub.db') as db:\n"
        "    if " + repr(create) + ":\n"
        "        db.execute('CREATE TABLE ro_stack_ci(value TEXT)')\n"
        "        db.execute(\"INSERT INTO ro_stack_ci VALUES ('synthetic')\")\n"
        "    assert db.execute('SELECT value FROM ro_stack_ci').fetchall() == [('synthetic',)]\n"
    )
    docker("exec", project + "-hub", "python", "-c", program)


def cleanup(project):
    filters = [f"label={CI_LABEL}={project}", f"label=com.docker.compose.project={project}"]
    failures = []
    for query in filters:
        try:
            ids = docker("ps", "-aq", "--filter", query).split()
            if ids:
                docker("rm", "-f", *ids)
            assert not docker("ps", "-aq", "--filter", query).strip()
        except (AssertionError, subprocess.CalledProcessError):
            failures.append("container")
    for object_type in ("network", "volume"):
        try:
            ids = docker(object_type, "ls", "-q", "--filter", filters[1]).split()
            if ids:
                docker(object_type, "rm", *ids)
            assert not docker(object_type, "ls", "-q", "--filter", filters[1]).strip()
        except (AssertionError, subprocess.CalledProcessError):
            failures.append(object_type)
    assert not failures, failures


def test_release(root, env, config, project):
    candidate = "router-orchestrator-hub:" + project
    image_dir = root / "candidate"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text(f"FROM {config['RO_IMAGE']}\nLABEL ci.test=synthetic\n")
    try:
        docker("build", "--pull=false", "--tag", candidate, str(image_dir))
        proposal = root / "candidate.env"
        write_env(proposal, dict(config, RO_IMAGE=candidate))
        update = ("update", "--candidate-env", proposal, "--acknowledge-schema-risk")
        # Kill the Manager while the candidate is ready, before the env commit.
        pid = os.fork()
        if pid == 0:
            try:
                with patch.object(Stack, "publish", side_effect=lambda _: os.kill(os.getpid(), signal.SIGKILL)):
                    cli(env, *update)
            finally:
                os._exit(1)
        _, state = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(state) and os.WTERMSIG(state) == signal.SIGKILL
        cli(env, "status", expected=2)
        cli(env, "recover")
        assert m.read_env(env, set(config))["RO_IMAGE"] == config["RO_IMAGE"]
        marker(project)
        cli(env, *update)
        assert m.read_env(env, set(config))["RO_IMAGE"] == candidate
        cli(env, "rollback")
        assert m.read_env(env, set(config))["RO_IMAGE"] == config["RO_IMAGE"]
        marker(project)
    finally:
        # Candidate may still be attached after a failure. Container cleanup runs first.
        cleanup(project)
        if m.call("docker", "image", "inspect", candidate, check=False).returncode == 0:
            docker("image", "rm", candidate)


class AdapterCheckFailed(Exception):
    """Only fixed identifiers and sanitized outcomes reach the CI log."""


def error_category(error):
    if isinstance(error, subprocess.CalledProcessError):
        return f"command_exit={error.returncode}"
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, ConnectionError):
        return "connection_failed"
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, sqlite3.Error):
        return "sqlite_error"
    if isinstance(error, ProfileError):
        return "profile_validation"
    if isinstance(error, m.Blocked):
        return "blocked"
    if isinstance(error, AssertionError):
        return "assertion_failed"
    return "unexpected_error"


def adapter_failure_sites(error):
    """Report only fixed exception labels and trusted source coordinates.

    Include the exception chain: rollback errors can otherwise hide the
    original publication failure. Never emit messages, locals or raw paths.
    """
    sources = {
        "tests/docker_stack_integration_ci.py",
        "docker/unified_transport_runtime.py",
        "docker/unified_transport_catalog.py",
        "docker/unified_xray_adapter.py",
        "docker/unified_core_manager.py",
        "docker/core_job_runner.py",
        "docker/xray_releases.py",
        "docker/unified_xray_state.py",
        "docker/secure_reverse_probe.py",
        "docker/secure_profile_store.py",
        "docker/secure_profile_catalog.py",
        "docker/transport_canary.py",
        "docker/transport_profiles.py",
        "docker/manager.py",
        "docker/stack_runtime.py",
        "docker/secure_ingress.py",
        "docker/secure_publication_transaction.py",
        "tools/rehearse_secure_reverse.py",
    }
    kinds = {cls: cls.__name__ for cls in (
        AttributeError, KeyError, TypeError, IndexError, NameError,
        UnboundLocalError, RuntimeError, ValueError, RecursionError,
        AssertionError, OSError, PermissionError, FileNotFoundError,
        TimeoutError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
        sqlite3.Error, sqlite3.OperationalError, sqlite3.IntegrityError,
        ProfileError, m.Blocked,
    )}
    seen = set()
    details = []
    while error is not None and id(error) not in seen and len(details) < 4:
        seen.add(id(error))
        frames = []
        tb = error.__traceback__
        while tb is not None:
            try:
                name = Path(tb.tb_frame.f_code.co_filename).relative_to(ROOT).as_posix()
            except ValueError:
                name = None
            if name in sources:
                frames.append(f"{name}:{tb.tb_lineno}")
            tb = tb.tb_next
        details.append(kinds.get(type(error), "other") + "@" +
                       ",".join(frames[-8:] or ["unknown"]))
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return ";".join(details)


@contextlib.contextmanager
def adapter_check(stage, check):
    try:
        yield
    except AdapterCheckFailed:
        raise
    except Exception as error:
        print("CI adapter exception sites: " + adapter_failure_sites(error),
              file=sys.stderr, flush=True)
        raise AdapterCheckFailed(f"stage={stage} check={check} error={error_category(error)}") from None


@contextlib.contextmanager
def adapter_detail(stage, check):
    # Preserve the real adapter's exception handling and IPC response.
    try:
        yield
    except Exception as error:
        print(f"stage={stage} check={check} error={error_category(error)}", file=sys.stderr, flush=True)
        raise


# Executed inside the real HUB container. Never emit response bodies, exceptions,
# or Python tracebacks. The host validates this record before forwarding it.
HUB_CHECKS = """
import argparse
import contextlib
import http.client
import json
import os
import runpy
import time
import uuid

@contextlib.contextmanager
def check(stage, name):
    global current
    current = (stage, name)
    yield

def fail(outcome):
    print("RO_ADAPTER_CHECK stage=%s check=%s %s" % (*current, outcome), flush=True)
    raise SystemExit(1)

def request(expected, method, path, body=None, auth=False, csrf=None, bearer=None, https=False):
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Cookie'] = hub['SESSION_COOKIE'] + '=' + hub['session_token']()
    if csrf is not None:
        headers['X-Unified-Xray-CSRF'] = csrf
    if bearer is not None:
        headers['Authorization'] = 'Bearer ' + bearer
    if https:
        headers['X-Forwarded-Proto'] = 'https'
    conn = http.client.HTTPConnection('127.0.0.1', int(os.environ['OWRT_REMOTE_PORT']), timeout=90)
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        with conn.getresponse() as reply:
            if reply.status != expected:
                fail(f'expected_http={expected} actual_http={reply.status}')
            return reply.read()
    finally:
        conn.close()

def rejected(op, **payload):
    try:
        ipc(op, **payload)
    except RuntimeError as error:
        if str(error) != "Xray adapter rejected the operation":
            fail("error=ipc_unavailable")
    else:
        fail("error=unexpected_success")

current = ("adapter-start", "hub-import")
"""


def hub_check(project, body):
    program = HUB_CHECKS + """
try:
    hub = runpy.run_path('/opt/owrt-remote/owrt-remote-hub.py')
    ipc = hub['unified_xray_request']
""" + "".join("    " + line + "\n" for line in body.splitlines()) + """
except AssertionError:
    fail("error=assertion_failed")
except TimeoutError:
    fail("error=timeout")
except (OSError, http.client.HTTPException):
    fail("error=connection_failed")
except RuntimeError as error:
    fail({
        "Xray adapter rejected the operation": "error=ipc_rejected",
        "Xray adapter unavailable": "error=ipc_unavailable",
        "Invalid Xray response": "error=ipc_invalid_response",
        "Xray adapter peer verification failed": "error=ipc_peer_rejected",
    }.get(str(error), "error=runtime_error"))
except (ValueError, KeyError, TypeError):
    fail("error=invalid_response")
except Exception:
    fail("error=unexpected_error")
"""
    try:
        docker("exec", project + "-hub", "python", "-c", program, timeout=300)
    except subprocess.CalledProcessError as error:
        # Do not forward stderr, command arguments, or arbitrary stdout. Only
        # identifiers declared by the local test body may be reported.
        declared = {("adapter-start", "hub-import")}
        declared.update(re.findall(r"^with check\('([a-z-]+)', '([a-z0-9-]+)'\):$", body, re.M))
        match = re.fullmatch(
            r"RO_ADAPTER_CHECK stage=([a-z-]+) check=([a-z0-9-]+) "
            r"(expected_http=[1-5][0-9]{2} actual_http=[0-9]{3}|"
            r"error=(?:assertion_failed|timeout|connection_failed|runtime_error|invalid_response|"
            r"unexpected_error|ipc_rejected|ipc_unavailable|ipc_invalid_response|"
            r"ipc_peer_rejected|unexpected_success))\n?",
            error.stdout or "",
        )
        if match and (match[1], match[2]) in declared:
            raise AdapterCheckFailed(f"stage={match[1]} check={match[2]} {match[3]}") from None
        raise


@contextlib.contextmanager
def adapter_process(runtime, adapter_type=Adapter, *, expect_crash=False, listen_timeout=5):
    with adapter_check("adapter-start", "fork"):
        pid = os.fork()
    if pid == 0:
        try:
            adapter_type(runtime.path).run()
        except Exception as error:
            print(f"stage=adapter-start check=child error={error_category(error)}",
                  file=sys.stderr, flush=True)
        finally:
            os._exit(1)
    status = None
    try:
        with adapter_check("adapter-start", "listen"):
            deadline = time.monotonic() + listen_timeout
            while True:
                waited, result = os.waitpid(pid, os.WNOHANG)
                if waited:
                    status = result
                    raise AdapterCheckFailed("stage=adapter-start check=listen error=child_exited")
                try:
                    with socket.socket(socket.AF_UNIX) as conn:
                        conn.settimeout(1)
                        conn.connect(socket_address(runtime))
                    break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError from None
                    time.sleep(0.1)
        yield
        if expect_crash:
            with adapter_check("sigkill-recovery", "child-signal"):
                # The IPC read returning EOF implies the crashing child closed
                # its end. Reap it without sending a signal that could mask a failure.
                deadline = time.monotonic() + 5
                while True:
                    waited, result = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = result
                        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    time.sleep(0.1)
    finally:
        if status is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def wrong_peer(runtime, uid):
    pid = os.fork()
    if pid:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        return
    try:
        os.setgroups([])
        os.setgid(m.HUB_GID)
        os.setuid(uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(4)
            conn.connect(socket_address(runtime))
            try:
                conn.sendall(b'{"op":"status"}\n')
                assert conn.recv(4096) == b""
            except (BrokenPipeError, ConnectionResetError):
                pass
    except BaseException:
        os._exit(1)
    os._exit(0)


def test_adapter(env, config, project):
    runtime = Stack(env, config)
    state = XrayState(runtime)
    with adapter_check("ipc-readiness", "upstream-image-volumes"):
        volumes = json.loads(docker("image", "inspect", "--format", "{{json .Config.Volumes}}",
                                    config["RO_XRAY_IMAGE"]))
        assert set(volumes or {}) == {"/usr/local/etc/xray", "/var/log/xray"}
        assert set(volumes) <= set(XRAY_TMPFS)
    port, ssh_port, edit_port, failing, abandoned, abandoned_ssh = choose_ports(6)
    crash_token = uuid.uuid4().hex

    runtime_fault = runtime.path.parent / ".ci-runtime-fault"

    class DiagnosticAdapter(Adapter):
        def open_project(self, **kwargs):
            with adapter_detail("ipc-peers", "active-project"):
                return super().open_project(**kwargs)

        @staticmethod
        def owned_xray(stack, release, record):
            with adapter_detail("ipc-readiness", "xray-ownership"):
                try:
                    return Adapter.owned_xray(stack, release, record)
                except m.Blocked as error:
                    for reason in ("identity", "runtime", "tmpfs", "bind-mounts"):
                        if str(error) == "Xray ownership verification failed: " + reason:
                            print("Xray ownership check failed:", reason, file=sys.stderr, flush=True)
                    raise

        def test_config(self, stack, release, data):
            with adapter_detail("xray-validation", "candidate"):
                return super().test_config(stack, release, data)

        def restart(self, stack, release, container, routers):
            with adapter_detail("xray-runtime", "restart"):
                super().restart(stack, release, container, routers)

    class FaultAdapter(DiagnosticAdapter):
        def restart(self, stack, release, container, routers):
            super().restart(stack, release, container, routers)
            if any(failing in router["ports"] for router in routers.values()):
                docker("stop", container)
                runtime_fault.touch()
                raise m.Blocked("CI injected runtime failure")

        def handle(self, stack, release, records, request):
            result = super().handle(stack, release, records, request)
            if request.get("transaction") == crash_token:
                os.kill(os.getpid(), signal.SIGKILL)
            return result

    with adapter_process(runtime, FaultAdapter, expect_crash=True):
        with adapter_check("ipc-peers", "wrong-uid"):
            wrong_peer(runtime, 65534)
        with adapter_check("ipc-peers", "same-uid-outside-hub"):
            wrong_peer(runtime, m.HUB_UID)
        with adapter_check("deployment-lock", "restart-denied"), m.deployment_lock():
            hub_check(project, """
with check('deployment-lock', 'restart-denied'):
    rejected('restart')
""")
        with adapter_check("hub-api", "exec"):
            hub_check(project, f"""
with check('web-auth', 'anonymous-status'):
    request(401, 'GET', '/api/xray/status')
with check('web-auth', 'restart-no-csrf'):
    request(403, 'POST', '/api/xray/restart', auth=True)
with check('web-auth', 'restart-malformed-csrf'):
    request(403, 'POST', '/api/xray/restart', auth=True, csrf=chr(255))
with check('web-auth', 'owner-csrf'):
    auth = json.loads(request(200, 'GET', '/api/auth/meta', auth=True))['auth']
    csrf = auth['unified_xray_csrf']
    assert auth['is_owner'] is True and isinstance(csrf, str) and csrf
with check('ipc-readiness', 'web-status'):
    assert json.loads(request(200, 'GET', '/api/xray/status', auth=True))['ready'] is True
with check('ipc-readiness', 'status'):
    assert ipc('status')['ready'] is True
with check('ipc-readiness', 'version'):
    assert ipc('version')['version'] == {os.environ["RO_CI_XRAY_VERSION"]!r}
with check('ipc-readiness', 'health'):
    assert ipc('health')['ready'] is True
with check('ipc-peers', 'operation-allowlist'):
    rejected('shell')
with check('ipc-peers', 'target-override'):
    rejected('restart', container='unowned')
data = {{'id': 'ci-router', 'entry_port': {port}, 'ssh_entry_port': {ssh_port}}}
with check('web-auth', 'register-no-csrf'):
    request(403, 'POST', '/api/router', data, auth=True)
with check('registration', 'create-wss'):
    request(200, 'POST', '/api/router', data, auth=True, csrf=csrf)
with check('editing', 'change-port'):
    request(200, 'POST', '/api/router/ci-router/xray/edit', {{'entry_port': {edit_port}}},
            auth=True, csrf=csrf)
def sqlite_state():
    with hub['connect']() as db:
        row = hub['get_active_router'](db, 'ci-router')
        return dict(row), db.execute('SELECT token FROM xray_receipt WHERE slot=1').fetchone()[0]
with check('editing', 'sqlite-before-rejection'):
    before = sqlite_state()
edit = '/api/router/ci-router/xray/edit'
with check('conflicts', 'duplicate-listener'):
    request(409, 'POST', edit, {{'entry_port': {ssh_port}}}, auth=True, csrf=csrf)
    assert sqlite_state() == before
with check('runtime-rollback', 'edit-rejected'):
    request(409, 'POST', edit, {{'entry_port': {failing}}}, auth=True, csrf=csrf)
with check('runtime-rollback', 'sqlite-unchanged'):
    assert sqlite_state() == before
with check('runtime-rollback', 'xray-ready-no-pending'):
    status = ipc('status')
    assert status['ready'] is True and status['pending'] is None
with check('runtime-rollback', 'sqlite-xray-parity'):
    row = before[0]
    assert row['entry_port'] == {edit_port} and row['vless_port'] == {runtime.plan["public"]}
    assert hub['make_router_xray_config'](row)['outbounds'][2]['streamSettings']['wsSettings']['path'] == {runtime.plan["wss_path"]!r}
    assert ipc('validate_config', router_id='ci-router', router=hub['unified_router_spec'](row))['changed'] is False
""")
        with adapter_check("runtime-rollback", "fault-exercised"):
            assert runtime_fault.is_file()
        with adapter_check("runtime-rollback", "config-restored"):
            record = state.load()
            assert record["pending"] is None
            assert state.config.read_bytes() == state.render(record, record["routers"])
        with adapter_check("deletion", "exec"):
            hub_check(project, """
with check('web-auth', 'retrieve-csrf'):
    csrf = json.loads(request(200, 'GET', '/api/auth/meta', auth=True))['auth']['unified_xray_csrf']
with check('deletion', 'delete-router'):
    result = json.loads(request(200, 'POST', '/api/router/ci-router/delete', auth=True, csrf=csrf))
    assert result['deleted'] is True
    with hub['connect']() as db:
        assert hub['get_active_router'](db, 'ci-router') is None
""")
        with adapter_check("deletion", "config-restored"):
            record = state.load()
            assert record["pending"] is None and not record["routers"]
            assert state.config.read_bytes() == record["base"].encode()
        with adapter_check("registration", "exec"):
            hub_check(project, f"""
with check('web-auth', 'retrieve-csrf'):
    csrf = json.loads(request(200, 'GET', '/api/auth/meta', auth=True))['auth']['unified_xray_csrf']
with check('registration', 'recreate-wss'):
    request(200, 'POST', '/api/router', {{'id': 'ci-router', 'entry_port': {port},
            'ssh_entry_port': {ssh_port}}}, auth=True, csrf=csrf)
with check('registration', 'authorized-restart'):
    request(200, 'POST', '/api/xray/restart', auth=True, csrf=csrf)
""")
        with adapter_check("registration", "listener"):
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                pass
        holds = runtime.state / ".xray-maintenance-hold"
        with adapter_check("hold", "restart-denied"):
            holds.touch()
            try:
                hub_check(project, """
with check('hold', 'status'):
    assert ipc('status')['maintenance_hold'] is True
with check('hold', 'restart-denied'):
    rejected('restart')
""")
            finally:
                holds.unlink()
        with adapter_check("sigkill-recovery", "prepare-without-receipt"):
            before = state.config.read_bytes()
            hub_check(project, f"""
with check('sigkill-recovery', 'prepare-without-receipt'):
    router = {{'id': 'abandoned', 'ports': [{abandoned}, {abandoned_ssh}],
              'clients': [str(uuid.uuid4()), str(uuid.uuid4())],
              'targets': [['127.0.0.1', 80], ['127.0.0.1', 22]]}}
    try:
        ipc('apply_config', router_id='abandoned', router=router, transaction={crash_token!r})
    except RuntimeError as error:
        assert str(error) in {{'Invalid Xray response', 'Xray adapter unavailable'}}
    else:
        fail('error=unexpected_success')
""")
    with adapter_check("sigkill-recovery", "durable-prepare"):
        assert state.load()["pending"]["token"] == crash_token
        assert state.config.read_bytes() != before
    with adapter_check("docker-manager", "pending-start-blocked"):
        cli(env, "start", expected=2, expected_error="BLOCKED: Pending Xray transaction: reconcile from HUB")
    with adapter_process(runtime, DiagnosticAdapter):
        with adapter_check("sigkill-recovery", "reconcile"):
            hub_check(project, f"""
with check('sigkill-recovery', 'reconcile'):
    with hub['_XRAY_RELOAD_LOCK'], hub['connect']() as db:
        hub['reconcile_unified_xray'](db)
        assert hub['get_router'](db, 'abandoned') is None
        row = hub['get_active_router'](db, 'ci-router')
        assert row['entry_port'] == {port} and row['ssh_entry_port'] == {ssh_port}
    assert ipc('health')['ready'] is True
""")
        with adapter_check("sigkill-recovery", "config-restored"):
            assert state.config.read_bytes() == before
            assert state.load()["pending"] is None
        with adapter_check("docker-manager", "validate-recovered"):
            cli(env, "validate")


def probe_log_categories(output):
    """Classify disposable fixture logs without exposing any raw text."""
    text = output.lower()
    patterns = {
        "tls-trust": ("unknown authority", "certificate signed", "certificate verify", "x509:"),
        "tls-handshake": ("tls handshake", "bad certificate", "remote error: tls"),
        "websocket": ("bad handshake", "websocket", "unexpected http status"),
        "permission": ("permission denied", "read-only file system"),
        "connection-refused": ("connection refused",),
        "network-timeout": ("i/o timeout", "context deadline exceeded"),
        "routing-denied": ("blocked by", "access denied", "rejected"),
        "config-error": ("failed to load config", "failed to build", "failed to start"),
        "reverse": ("reverse",),
        "eof": ("unexpected eof", "connection reset",),
    }
    return ",".join(label for label, terms in patterns.items()
                    if any(term in text for term in terms)) or "none-recognized"


def fixture_probe_summary(container):
    """Only fixed status values and log categories leave the CI runner."""
    try:
        base = ("docker", "--host", "unix:///var/run/docker.sock")
        status = m.call(*base, "inspect", "--format", "{{.State.Status}}", container,
                        check=False, env=m.local_docker_env(), timeout=5)
        state = status.stdout.strip()
        if status.returncode or state not in {
            "created", "running", "paused", "restarting", "removing", "exited", "dead",
        }:
            state = "unavailable"
        logs = m.call(*base, "logs", "--tail", "100", container, check=False,
                      env=m.local_docker_env(), timeout=5)
        categories = ("unavailable" if logs.returncode else
                      probe_log_categories(logs.stdout + "\n" + logs.stderr))
        return f"state={state} categories={categories}"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "state=unavailable categories=unavailable"


def core_candidate(environ):
    reference = environ.get("RO_CI_CORE_CANDIDATE_IMAGE", "")
    version = environ.get("RO_CI_CORE_CANDIDATE_VERSION", "")
    if (not PINNED_CORE.fullmatch(reference) or not CORE_VERSION.fullmatch("v" + version) or
            reference == environ.get("RO_CI_XRAY_IMAGE") or version == environ.get("RO_CI_XRAY_VERSION")):
        raise SystemExit("Core acceptance blocked: a distinct, reviewed official Linux amd64 Xray image is required")
    return reference, version


def test_core_restart(stack, project, profile_id):
    """Run a real Xray restart with two reverse channels and a fixture heartbeat."""
    state = XrayState(stack)
    transport = state.load()
    entry, = [item for item in transport["profiles"] if item["profile"]["name"] == profile_id]
    profile = entry["profile"]
    assignment = entry["assignments"]["ci-router"]
    portals = assignment["ports"]
    config = json.loads(state.config.read_bytes())
    clients = {client["id"]: client["reverse"]["tag"]
               for inbound in config["inbounds"] for client in inbound.get("settings", {}).get("clients", [])
               if client["id"] in assignment["clients"]}
    version = "Xray " + os.environ["RO_CI_XRAY_VERSION"]
    release = json.loads(stack.journal.read_text())["current"]
    before = stack.inspect(release)
    ingress = project + "-fixture-ingress"
    external = stack.plan["mode"] != "standalone"

    def identity(item):
        return item["Id"], item["Image"], item["State"]["StartedAt"]

    def ingress_identity():
        item, = json.loads(docker("container", "inspect", ingress))
        return identity(item)

    ingress_before = ingress_identity() if external else None
    nginx_files = [path for path in (stack.plan["nginx_conf"],
                                     stack.path.parent / "fixture-nginx.conf")
                   if path is not None and path.exists()]
    nginx_before = {path: m.file_hash(path) for path in nginx_files}

    class LuCI(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            if self.request.recv(4096).startswith(b"GET /cgi-bin/luci/ "):
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nLuCI")

    class SSH(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"SSH-2.0-RouterOrchestratorCI\r\n")

    with contextlib.ExitStack() as resources:
        targets = []
        for handler in (LuCI, SSH):
            server = resources.enter_context(socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            resources.callback(server.shutdown)
            targets.append(server.server_address[1])
        _, client = build_probe(profile, ingress_port=stack.plan["public"],
                                admin_port=portals[0], ssh_port=portals[1],
                                admin_target=targets[0], ssh_target=targets[1], core_version=version,
                                proxy_protocol=False)
        outbounds = [item for item in client["outbounds"] if item["protocol"] == "vless"]
        for outbound, identity in zip(outbounds, assignment["clients"]):
            old = outbound["settings"]["reverse"]["tag"]
            outbound["settings"]["id"] = identity
            outbound["settings"]["reverse"]["tag"] = clients[identity]
            for rule in client["routing"]["rules"]:
                if rule["inboundTag"] == [old]:
                    rule["inboundTag"] = [clients[identity]]
        directory = Path(resources.enter_context(tempfile.TemporaryDirectory(dir=stack.path.parent)))
        document = directory / "client.json"
        document.write_text(json.dumps(fixture_tls_client(client)))
        os.chown(document, 0, m.HUB_GID)
        document.chmod(0o640)
        name = project + "-core-ci-client"
        tls = stack.config["RO_TLS_DIR"]
        ca = profile["certificate_file"]
        docker("run", "-d", "--pull=never", "--name", name, "--label", f"{CI_LABEL}={project}",
               "--network=host", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
               "--user", f"{m.HUB_UID}:{m.HUB_GID}", "--env", "SSL_CERT_FILE=" + ca,
               "--mount", f"type=bind,src={document},dst=/probe.json,readonly",
               "--mount", f"type=bind,src={tls},dst=/etc/router-orchestrator/tls,readonly",
               *(arg for path, options in XRAY_TMPFS.items() for arg in ("--tmpfs", path + ":" + options)),
               stack.config["RO_XRAY_IMAGE"], "run", "-config", "/probe.json")
        resources.callback(docker, "rm", "-f", name)
        deadline = time.monotonic() + 30
        while True:
            try:
                core_baseline(stack, transport)
                break
            except m.Blocked:
                if time.monotonic() >= deadline:
                    for role, container in (("client", name), ("server", stack.name + "-xray")):
                        print(f"CI core reverse {role}: " + fixture_probe_summary(container),
                              file=sys.stderr, flush=True)
                    raise AssertionError("Core fixture reverse channels unavailable")
                time.sleep(0.5)
        heartbeat_marker = stack.state / ".ci-core-restored"
        resources.callback(heartbeat_marker.unlink, missing_ok=True)

        def run_core_job(action, version="", *, expected="success"):
            hub_check(project, f"""
with check('core-lifecycle', '{action}-{expected}-and-fleet-recovery'):
    job = ipc('core.start', action={action!r}, version={version!r}, confirmed=True)
    deadline = time.monotonic() + 175
    while time.monotonic() < deadline:
        # The heartbeat is synthetic. Reverse LuCI and SSH travel through Xray.
        if {expected == "success"!r} or os.path.isfile('/var/lib/owrt-remote/.ci-core-restored'):
            with hub['connect']() as db:
                db.execute('update routers set last_seen=? where id=?', (int(time.time()), 'ci-router'))
        try:
            result = ipc('core.job', job_id=job['job_id'])['job']
        except RuntimeError as error:
            if str(error) not in {{'Xray adapter rejected the operation', 'Invalid Xray response'}}:
                raise
            time.sleep(1)
            continue
        if result['status'] not in {{'pending', 'running'}}:
            assert result['status'] == {expected!r}
            break
        time.sleep(1)
    else:
        fail('error=timeout')
""")
            deadline = time.monotonic() + 5
            while True:
                try:
                    with m.deployment_lock():
                        return
                except m.Blocked:
                    assert time.monotonic() < deadline, "Core worker did not release its lock"
                    time.sleep(0.1)

        run_core_job("restart")
        after = stack.inspect(release)
        assert before["xray"]["State"]["StartedAt"] != after["xray"]["State"]["StartedAt"]
        assert before["xray"]["Id"] == after["xray"]["Id"]

        def assert_unchanged_services(items):
            for service in stack.services:
                if service != "xray":
                    assert identity(before[service]) == identity(items[service])
            if external:
                assert ingress_identity() == ingress_before
            assert all(m.file_hash(path) == value for path, value in nginx_before.items())

        def committed(status):
            record = json.loads(stack.journal.read_text())
            assert record["pending"] is None and record["core_receipt"]["status"] == status
            from core_job_runner import unified_directory
            job = unified_directory(stack) / (record["core_receipt"]["id"] + ".json")
            assert json.loads(job.read_text())["status"] == status
            return record["current"]

        assert_unchanged_services(after)
        alternative, version = core_candidate(os.environ)
        metadata, = json.loads(docker("image", "inspect", alternative))
        assert metadata["Os"] == "linux" and metadata["Architecture"] == "amd64"
        assert alternative in metadata["RepoDigests"]
        version_text = docker("run", "--rm", "--pull=never", "--network=none", alternative, "version")
        assert version_text.splitlines()[0].split()[:2] == ["Xray", version]
        candidate_id = metadata["Id"]
        assert candidate_id != release["images"]["xray"]
        directory = preparation_directory(stack)
        directory.mkdir(mode=0o700)
        manifest = {"version": "v" + version, "image": "router-orchestrator-xray:" + version,
                    "image_id": candidate_id, "official_digest_ref": alternative,
                    "official_image": "ghcr.io/xtls/xray-core:" + version, "validation": "config-only"}
        m.durable_write(directory / (version + ".json"), json.dumps(manifest))
        run_core_job("upgrade", "v" + version)
        updated = committed("success")
        items = stack.inspect(updated)
        assert items["xray"]["Id"] != after["xray"]["Id"]
        assert items["xray"]["Image"] == candidate_id
        assert_unchanged_services(items)
        run_core_job("rollback")
        restored = committed("success")
        items = stack.inspect(restored)
        assert items["xray"]["Image"] == release["images"]["xray"]
        assert_unchanged_services(items)
        original_id = items["xray"]["Id"]

        def reject_candidate(done):
            disrupted = False
            while not done.is_set():
                try:
                    observed = stack.inspect(restored, updated).get("xray")
                except subprocess.CalledProcessError:
                    # Compose can replace a container between Docker list and inspect.
                    done.wait(0.25)
                    continue
                if observed and observed["Image"] == candidate_id and not disrupted:
                    # Cover the empty Xray slot as well as an exited candidate across the matrix.
                    if stack.plan["mode"] == "external-only":
                        docker("rm", "-f", observed["Id"])
                    else:
                        docker("stop", "--time", "0", observed["Id"])
                    disrupted = True
                if (disrupted and observed and observed["Image"] == release["images"]["xray"] and
                        observed["Id"] != original_id and observed["State"]["Running"]):
                    # Only the recovered runtime gets a fresh fixture heartbeat.
                    heartbeat_marker.touch(mode=0o644)
                    return
                done.wait(0.25)
            raise AssertionError("Candidate was not disrupted and recovered")

        done = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            fault = pool.submit(reject_candidate, done)
            try:
                run_core_job("upgrade", "v" + version, expected="failed")
            finally:
                done.set()
                fault.result()
        assert committed("failed") == restored
        items = stack.inspect(restored)
        assert items["xray"]["Image"] == release["images"]["xray"]
        assert items["xray"]["Id"] != original_id
        assert_unchanged_services(items)


def test_profile_lifecycle(env, config, project, backend_port, transport="wss", target_port=None):
    stack = Stack(env, config)
    state = XrayState(stack)
    baseline = json.loads(state.config.read_bytes())
    original_nginx = stack.plan["nginx_conf"]
    original_nginx = original_nginx.read_bytes() if original_nginx is not None else None
    external_nginx = env.parent / "fixture-nginx.conf"
    external_bytes = external_nginx.read_bytes() if external_nginx.exists() else None
    name = "lifecycle-" + transport
    host = LIFECYCLE_HOST if transport == "wss" else f"{transport}.{LIFECYCLE_HOST}"
    profile = {"name": name, "transport": transport, "server_name": host, "port": backend_port}
    if transport == "reality-tcp":
        profile.update(target_host="127.0.0.1", target_port=target_port, server_names=[host])
    elif transport == "grpc":
        profile["service_name"] = "lifecycle_ci"
    else:
        profile["path"] = "/lifecycle/"
    class FixtureTrustAdapter(Adapter):
        def test_config(self, stack, release, data):
            with adapter_detail("profile-lifecycle", "xray-config"):
                return super().test_config(stack, release, data)

        def restart(self, stack, release, container, routers, *, extra_ports=None):
            with adapter_detail("profile-lifecycle", "xray-restart"):
                return super().restart(stack, release, container, routers, extra_ports=extra_ports)

        def handle(self, stack, release, records, request):
            probe_id = None
            summaries = {}

            def docker_with_fixture_ca(*args, **kwargs):
                nonlocal probe_id
                if args[:3] == ("rm", "-f", "-v") and len(args) == 4 and args[3] == probe_id:
                    summaries["client"] = fixture_probe_summary(probe_id)
                    summaries["server"] = fixture_probe_summary(stack.name + "-xray")
                if args[:2] == ("run", "-d") and transport != "reality-tcp":
                    certificate = f"/etc/router-orchestrator/tls/{host}/fullchain.pem"
                    tls = stack.config["RO_TLS_DIR"]
                    options = ("--mount", f"type=bind,src={tls},dst=/etc/router-orchestrator/tls,readonly",
                               "--env", "SSL_CERT_FILE=" + certificate)
                    result = docker(args[0], *options, *args[1:], **kwargs)
                else:
                    result = docker(*args, **kwargs)
                if args[:2] == ("run", "-d") and re.fullmatch(r"[a-f0-9]{64}", result.strip()):
                    probe_id = result.strip()
                return result

            def build_fixture_probe(*args, **kwargs):
                server, client = build_probe(*args, **kwargs)
                return server, fixture_tls_client(client)

            with (
                patch("unified_xray_adapter.docker", docker_with_fixture_ca),
                patch("unified_transport_runtime.build_probe", build_fixture_probe),
            ):
                try:
                    return super().handle(stack, release, records, request)
                except Exception as error:
                    operation = request.get("op")
                    if isinstance(operation, str) and operation in {
                        "transport.draft", "transport.stage", "transport.approve", "transport.publish",
                        "transport.disable", "transport.rollback", "transport.activate",
                    }:
                        if isinstance(error, ProfileError) and str(error) == (
                            "current stage must be approved before publication"
                        ):
                            raise
                        outcome = error_category(error)
                        if isinstance(error, m.Blocked) and str(error) == "Ingress reverse probe failed":
                            outcome = "reverse_proof_failed"
                            for role in ("client", "server"):
                                print(f"CI reverse {role}: " + summaries.get(role, "unavailable"),
                                      file=sys.stderr, flush=True)
                        print(f"stage=profile-lifecycle check={operation.removeprefix('transport.')} "
                              f"error={outcome}", file=sys.stderr, flush=True)
                        print("CI adapter exception sites: " + adapter_failure_sites(error),
                              file=sys.stderr, flush=True)
                    raise

    with adapter_process(stack, FixtureTrustAdapter):
        hub_check(project, f"""
base = '/api/secure-profiles/'
ref = {{'profile_id':{name!r}, 'revision':1}}
with check('profile-lifecycle', 'owner-and-csrf'):
    request(401, 'POST', base + 'disable', {{**ref, 'confirmed': True}})
    request(403, 'POST', base + 'disable', {{**ref, 'confirmed': True}}, auth=True)
with check('profile-lifecycle', 'draft'):
    ipc('transport.draft', profile={profile!r},
        remote_host='127.0.0.1', label='Lifecycle CI', public_port={stack.plan['public']})
with check('profile-lifecycle', 'stage'):
    stage = ipc('transport.stage', **ref)['stage']
approval = dict(ref, stage_sha256=stage['stage_sha256'], confirmed=True)
with check('profile-lifecycle', 'reject-unapproved-publish'):
    rejected('transport.publish', **approval)
with check('profile-lifecycle', 'approve'):
    assert ipc('transport.approve', **approval)['stage']['approved_at']
with check('profile-lifecycle', 'publish'):
    result = ipc('transport.publish', **approval)
    assert result['active'] and result['published']
with check('profile-lifecycle', 'disable-and-rollback'):
    assert not ipc('transport.disable', **ref, confirmed=True)['active']
    assert ipc('transport.rollback', **ref, confirmed=True)['active']
with check('profile-lifecycle', 'reactivate'):
    assert not ipc('transport.disable', **ref, confirmed=True)['active']
    assert ipc('transport.activate', **ref, confirmed=True)['active']
""")
    installed = json.loads(state.config.read_bytes())
    assert installed["inbounds"][:len(baseline["inbounds"])] == baseline["inbounds"]
    assert not installed["inbounds"][-1]["settings"]["clients"], "Probe credentials survived"
    if transport != "wss":
        with adapter_process(stack):
            hub_check(project, f"""
with check('profile-lifecycle', 'disable'):
    assert not ipc('transport.disable', profile_id={name!r}, revision=1, confirmed=True)['active']
""")
        assert json.loads(state.config.read_bytes()) == baseline
        return
    with adapter_process(stack):
        hub_check(project, """
with check('profile-assignment', 'bind-router-identity'):
    auth = json.loads(request(200, 'GET', '/api/auth/meta', auth=True))['auth']
    csrf = auth['unified_xray_csrf']
    router = {'id': 'ci-router'}
    issue = {'router_id': router['id'], 'confirmed': True}
    token = json.loads(request(200, 'POST', '/api/secure-profiles/identity', issue,
                               auth=True, csrf=csrf, https=True))['router_token']
    request(401, 'POST', '/api/agent/transport-profiles', router, bearer=token, https=True)
    confirmation = '/api/agent/transport-identity'
    assert json.loads(request(200, 'POST', confirmation, router, bearer=token, https=True))['identity_accepted']
    request(200, 'POST', '/api/heartbeat', router, bearer=token, https=True)
    rotated = json.loads(request(200, 'POST', '/api/secure-profiles/identity', issue,
                                 auth=True, csrf=csrf, https=True))['router_token']
    request(200, 'POST', '/api/heartbeat', router, bearer=token, https=True)
    request(200, 'POST', confirmation, router, bearer=rotated, https=True)  # discard first confirmation
    assert json.loads(request(200, 'POST', confirmation, router, bearer=rotated, https=True))['identity_accepted']
    request(401, 'POST', '/api/heartbeat', router, bearer=token, https=True)
    token = rotated
    request(401, 'POST', '/api/heartbeat', router, bearer=hub['agent_token'](), https=True)
with check('profile-assignment', 'individual-credentials'):
    ref = {'profile_id': 'lifecycle-wss', 'revision': 1}
    assign = {**issue, 'profiles': [ref]}
    request(200, 'POST', '/api/secure-profiles/assign', assign, auth=True, csrf=csrf)
    request(401, 'POST', '/api/agent/transport-profiles', router, bearer=hub['agent_token'](), https=True)
    request(401, 'POST', '/api/agent/transport-profiles', {'id':'other-router'}, bearer=token, https=True)
    profile, = json.loads(request(200, 'POST', '/api/agent/transport-profiles',
                                  router, bearer=token, https=True))['profiles']
    assert {key: profile[key] for key in ref} == ref
    outbounds = profile['outbounds']
    assert len(outbounds) == 2 and outbounds[0]['settings']['id'] != outbounds[1]['settings']['id']
    rejected('transport.disable', **ref, confirmed=True)
with check('profile-assignment', 'transport-task-replay'):
    target = {**ref, 'assignment_id': profile['assignment_id']}
    action = {**issue, 'action': 'stage', 'slot': '0', 'target': target}
    owner_task = '/api/secure-profiles/agent_action'
    agent_task = '/api/agent/transport-task'
    request(400, 'POST', owner_task, {**action, 'target': {**target, 'revision': 2}}, auth=True, csrf=csrf)
    request(202, 'POST', owner_task, action, auth=True, csrf=csrf)
    claim = {**router, 'protocol': 2}
    first = json.loads(request(200, 'POST', agent_task, claim, bearer=token, https=True))['task']
    assert first == json.loads(request(200, 'POST', agent_task, claim, bearer=token, https=True))['task']
    assert first['target'] == target
    ack = {**router, 'task_id': first['task_id'], 'target': target, 'succeeded': True}
    request(409, 'POST', agent_task, {**ack, 'target': None}, bearer=token, https=True)
    request(200, 'POST', agent_task, ack, bearer=token, https=True)
    request(200, 'POST', agent_task, ack, bearer=token, https=True)
    assert json.loads(request(200, 'POST', agent_task, claim, bearer=token, https=True))['task'] is None
""")
        with adapter_check("core-lifecycle", "fixture"):
            test_core_restart(stack, project, name)
        hub_check(project, """
with check('profile-assignment', 'remove-fixture-assignment'):
    assert ipc('transport.unassign', router_id='ci-router', confirmed=True)['applied']
""")
    installed = json.loads(state.config.read_bytes())

    class CrashAdapter(Adapter):
        def restart(self, stack, release, container, routers, *, extra_ports=None):
            super().restart(stack, release, container, routers, extra_ports=extra_ports)
            if extra_ports == ():
                os.kill(os.getpid(), signal.SIGKILL)

    with adapter_process(stack, CrashAdapter, expect_crash=True):
        hub_check(project, """
with check('profile-recovery', 'crash-after-xray-restart'):
    try:
        ipc('transport.disable', profile_id='lifecycle-wss', revision=1, confirmed=True)
    except RuntimeError as error:
        assert str(error) in {'Invalid Xray response', 'Xray adapter unavailable'}
    else:
        fail('error=unexpected_success')
""")
    assert state.load()["profile_pending"] and state.config.read_bytes() != encode(installed)
    with adapter_process(stack, listen_timeout=45):
        hub_check(project, """
with check('profile-recovery', 'catalog-and-runtime-recovered'):
    assert ipc('status')['ready']
    revisions = ipc('transport.list')['revisions']
    assert next(item for item in revisions if item['profile_id'] == 'lifecycle-wss')['active']
""")

    class CommitCrashAdapter(Adapter):
        def handle(self, stack, release, records, request):
            if request.get("op") != "transport.disable":
                return super().handle(stack, release, records, request)
            save = XrayState.save

            def crash_before_final_state(instance, record):
                if record.get("profile_committed") and not record.get("profile_pending"):
                    os.kill(os.getpid(), signal.SIGKILL)
                save(instance, record)

            with patch.object(XrayState, "save", crash_before_final_state):
                return super().handle(stack, release, records, request)

    with adapter_process(stack, CommitCrashAdapter, expect_crash=True):
        hub_check(project, """
with check('profile-recovery', 'crash-after-catalog-commit'):
    try:
        ipc('transport.disable', profile_id='lifecycle-wss', revision=1, confirmed=True)
    except RuntimeError as error:
        assert str(error) in {'Invalid Xray response', 'Xray adapter unavailable'}
    else:
        fail('error=unexpected_success')
""")
    assert state.load()["profile_pending"]
    with adapter_process(stack, listen_timeout=45):
        hub_check(project, """
with check('profile-recovery', 'committed-disable-recovered'):
    assert ipc('status')['ready']
    revisions = ipc('transport.list')['revisions']
    disabled = next(item for item in revisions if item['profile_id'] == 'lifecycle-wss')
    assert disabled['published'] and not disabled['active']
""")
    assert json.loads(state.config.read_bytes()) == baseline
    if original_nginx is not None:
        assert stack.plan["nginx_conf"].read_bytes() == original_nginx
    if external_bytes is not None:
        assert external_nginx.read_bytes() == external_bytes


def test_additional_profiles(env, config, project):
    backend, target = choose_ports(2)
    tls = env.parent / "tls" / f"reality-tcp.{LIFECYCLE_HOST}"
    with subprocess.Popen(
            ["openssl", "s_server", "-accept", f"127.0.0.1:{target}", "-tls1_3", "-alpn", "h2",
             "-cert", str(tls / "fullchain.pem"), "-key", str(tls / "privkey.pem"), "-quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as server:
        try:
            test_profile_lifecycle(env, config, project, backend, "reality-tcp", target)
        finally:
            server.terminate()
            server.wait(timeout=5)
    for transport in ("xhttp", "grpc"):
        test_profile_lifecycle(env, config, project, choose_ports(1)[0], transport)


def main():
    assert os.geteuid() == 0 and os.getenv("GITHUB_ACTIONS") == "true"
    assert os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
    mode = sys.argv[1]
    assert mode in {"standalone", "external-stream", "external-only"}
    project = "ro-stack-ci-" + uuid.uuid4().hex[:12]
    backend = os.environ.get("RO_CI_BACKEND", "external_readonly")
    assert backend in {"external_readonly", "unified_adapter"}
    if backend == "unified_adapter":
        core_candidate(os.environ)
    nginx, xray = os.environ["RO_CI_NGINX_IMAGE"], os.environ["RO_CI_XRAY_IMAGE"]

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)

    with tempfile.TemporaryDirectory(prefix=project + "-") as directory:
        root = Path(directory)
        hub_image = "router-orchestrator-hub:" + m.git_sha()[:12]
        assert m.call("docker", "image", "inspect", hub_image, check=False).returncode != 0
        env = root / "stack.env"
        try:
            assert m.build_image() == hub_image
            config, plan, module = fixture(root, mode, project, hub_image, nginx, xray, backend)
            write_env(env, config)
            with patch.object(m, "LOCK", root / ".deploy.lock"), \
                 patch.object(m, "HUB_DEPLOY_ROOT", root), \
                 patch.object(Stack, "preflight", diagnose_gate(Stack.preflight)), \
                 patch.object(Stack, "await_ready", diagnose_gate(Stack.await_ready)):
                cli(env, "validate")
                cli(env, "start")
                expected = {"hub": True, "xray": True}
                if mode != "external-only":
                    expected["nginx"] = True
                assert json.loads(cli(env, "status")) == expected
                profile_port = choose_ports(1)[0] if backend == "unified_adapter" else None
                if mode != "standalone":
                    external_fixture(root, config, plan, module, project, profile_port)
                verify_ingress(root, plan)
                marker(project, create=True)
                cli(env, "stop")
                cli(env, "start")
                marker(project)
                docker("stop", project + "-xray")
                failed = json.loads(cli(env, "status", expected=2))
                assert failed == dict(expected, xray=False)
                cli(env, "recover")
                marker(project)
                verify_ingress(root, plan)
                if backend == "unified_adapter":
                    with adapter_check("adapter-start", "fixture"):
                        test_adapter(env, config, project)
                    with adapter_check("profile-lifecycle", "fixture"):
                        test_profile_lifecycle(env, config, project, profile_port)
                        if mode == "standalone":
                            test_additional_profiles(env, config, project)
                    with adapter_check("stack-backup", "private-catalog-and-journal"):
                        verify_stack_backup(root, env, config)
                    verify_secure_reverse(root, config, module, project)
                if mode == "standalone":
                    with adapter_check("docker-manager", "release-recovery-and-rollback"):
                        test_release(root, env, config, project)
        finally:
            try:
                cleanup(project)
            finally:
                if m.call("docker", "image", "inspect", hub_image, check=False).returncode == 0:
                    docker("image", "rm", hub_image)
    print("Real Docker integration passed:", mode, backend)


if __name__ == "__main__":
    try:
        main()
    except AdapterCheckFailed as error:
        raise SystemExit(f"CI unified adapter failed: {error}") from None
    except subprocess.CalledProcessError as error:
        # Never print runtime logs, environment or config contents.
        raise SystemExit(f"Integration command failed with exit status {error.returncode}") from None
