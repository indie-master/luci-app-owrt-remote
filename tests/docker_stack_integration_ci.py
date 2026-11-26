"""Real unified-stack checks restricted to disposable GitHub-hosted runners."""
import contextlib
from functools import wraps
import io
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
import manager as m
from stack_runtime import Ingress, Stack, docker, websocket
from orchestrator_stack_config import STREAM_MODULE, render_external, render_internal, stack_plan

HUB = "hub.example.invalid"
PASSTHROUGH = "pass.example.invalid"
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


def cli(path, *args, expected=0):
    output = io.StringIO()
    with patch.object(sys, "argv", ["manager.py", "stack", "--env-file", str(path), *map(str, args)]):
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = m.main()
    assert code == expected, (args[0], code, expected)
    return output.getvalue()


def choose_ports(count):
    with contextlib.ExitStack() as sockets:
        listeners = [sockets.enter_context(socket.socket()) for _ in range(count)]
        for sock in listeners:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in listeners]


def cert(directory, host):
    directory.mkdir()
    m.call("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
           "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
           "-keyout", str(directory / "privkey.pem"), "-out", str(directory / "fullchain.pem"))
    os.chown(directory / "privkey.pem", 0, m.HUB_GID)
    (directory / "privkey.pem").chmod(0o640)


def write_env(path, config):
    m.durable_write(path, "".join(f"{key}={value}\n" for key, value in config.items()))


def fixture(root, mode, project, hub_image, nginx, xray):
    hub, ws, stream, tls, health, public, http = choose_ports(7)
    cert(root / "tls", HUB)
    cert(root / "tls" / "passthrough", PASSTHROUGH)
    (root / "xray").mkdir()
    (root / "assets").mkdir()

    settings = {"clients": [{"id": str(uuid.uuid4())}], "decryption": "none"}
    loopback = {"listen": "127.0.0.1", "protocol": "vless", "settings": settings}
    xray_tls = "/etc/router-orchestrator/tls/passthrough/"
    xray_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            dict(loopback, port=ws, streamSettings={"network": "ws", "wsSettings": {"path": WS_PATH}}),
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
        "RO_WSS_PORT": str(ws), "RO_WSS_PATH": WS_PATH, "RO_IMAGE": hub_image,
        "RO_STATE_DIR": str(root / "data"), "RO_PUBLIC_URL": f"https://{HUB}:{public}",
        "RO_XRAY_BACKEND": "external_readonly", "RO_XRAY_IMAGE": xray,
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


def external_fixture(root, config, plan, module, project):
    paths = render_external(plan)
    prefix = f"load_module {module};\n" if module else ""
    prefix += "events {}\npid /var/run/nginx.pid;\n"
    rules = paths["external-stream.snippet"]
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
            assert websocket(Ingress(HUB, plan["public"], hub_tls), HUB, WS_PATH)
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


def main():
    assert os.geteuid() == 0 and os.getenv("GITHUB_ACTIONS") == "true"
    assert os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
    mode = sys.argv[1]
    assert mode in {"standalone", "external-stream", "external-only"}
    project = "ro-stack-ci-" + uuid.uuid4().hex[:12]
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
            config, plan, module = fixture(root, mode, project, hub_image, nginx, xray)
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
                if mode != "standalone":
                    external_fixture(root, config, plan, module, project)
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
                if mode == "standalone":
                    test_release(root, env, config, project)
        finally:
            try:
                cleanup(project)
            finally:
                if m.call("docker", "image", "inspect", hub_image, check=False).returncode == 0:
                    docker("image", "rm", hub_image)
    print("Real Docker integration passed:", mode)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Never print runtime logs, environment or config contents.
        raise SystemExit(f"Integration command failed with exit status {error.returncode}") from None
