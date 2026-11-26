"""Real Docker lifecycle test for the disposable GitHub-hosted runner."""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "docker"))
import manager
from test_orchestrator_stack_config import fixture as stack_fixture


def cli(*args, expected=0):
    with patch.object(sys, "argv", ["manager.py", *map(str, args)]):
        code = manager.main()
    if code != expected:
        raise AssertionError(f"{args[0]} exit status {code}, expected {expected}")


def docker(*args):
    return manager.call("docker", *args).stdout.strip()


def assert_api(port):
    base = f"http://127.0.0.1:{port}"
    for path in ("/health", "/api/auth/meta"):
        with urlopen(base + path, timeout=5) as response:
            assert response.status == 200 and json.load(response)["ok"] is True
    try:
        with urlopen(base + "/api/routers", timeout=5):
            raise AssertionError("Fleet API allowed an anonymous request")
    except HTTPError as error:
        assert error.code == 401


def database(state):
    return contextlib.closing(sqlite3.connect(state / "hub.db"))


def assert_data(state):
    with database(state) as db:
        assert db.execute("SELECT value FROM ro_ci_marker").fetchall() == [("synthetic-row",)]
    assert (state / "ci-fixtures" / "aux.txt").read_text() == "synthetic-file\n"


def fixture_image(root, base, tag, instruction):
    source = root / "image"
    source.mkdir(exist_ok=True)
    (source / "Dockerfile").write_text(f"FROM {base}\n{instruction}\n")
    docker("build", "--pull=false", "--tag", tag, str(source))


def interrupt_after_env(candidate):
    pid = os.fork()
    if pid == 0:
        original = manager.write_image

        def write_then_kill(image):
            original(image)
            os.kill(os.getpid(), signal.SIGKILL)

        try:
            with patch.object(manager, "write_image", write_then_kill), \
                 patch.object(manager, "build_image", return_value=candidate):
                cli("upgrade", "--acknowledge-schema-risk")
        finally:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert manager.pending_transaction()["phase"] == "switching"
    cli("backup", expected=2)
    cli("recover")


def resources(project):
    return {
        "containers": docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"),
        "networks": docker("network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"),
        "volumes": docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"),
    }


def cleanup(project, config, images):
    try:
        manager.compose("down", "--volumes", "--remove-orphans", config=config)
        assert not any(resources(project).values()), "Compose left project resources behind"
    finally:
        remaining = []
        for image in reversed(images):
            if manager.call("docker", "image", "inspect", image, check=False).returncode == 0:
                removed = manager.call("docker", "image", "rm", image, check=False)
                if removed.returncode != 0 or manager.call(
                        "docker", "image", "inspect", image, check=False).returncode == 0:
                    remaining.append(image)
        assert not remaining, f"Could not remove CI images: {remaining}"


def validate_stack_models(root):
    for mode, expected in (
        ("standalone", {"hub", "xray", "nginx"}),
        ("external-stream", {"hub", "xray", "nginx"}),
        ("external-only", {"hub", "xray"}),
    ):
        mode_dir = root / mode
        mode_dir.mkdir()
        config = stack_fixture(mode_dir, mode)
        config["RO_STACK_NAME"] = root.name + "-" + mode
        env_file = mode_dir / "compose.env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in config.items()))
        manager.call(sys.executable, str(REPO / "tools/orchestrator_stack_config.py"),
                     "--env-file", str(env_file), "--output-dir", str(mode_dir / "generated"))
        model = json.loads(docker("compose", "--file", str(REPO / "docker-compose.yml"),
                                  "--env-file", str(env_file), "--project-name", config["RO_STACK_NAME"],
                                  "--profile", mode, "config", "--format", "json"))
        assert set(model["services"]) == expected, f"Unexpected services in {mode}"
        for service_name, service in model["services"].items():
            assert service["container_name"] == config["RO_STACK_NAME"] + "-" + service_name
            assert all("docker.sock" not in mount["source"] for mount in service.get("volumes", []))
        assert model["services"]["xray"]["image"] == config["RO_XRAY_IMAGE"]


def run(root):
    project = root.name
    assert project.startswith("ro-ci-") and not any(resources(project).values())
    validate_stack_models(root)
    manager.PROJECT = project
    manager.ENV = root / ".env"
    manager.LOCK = root / ".lock"
    manager.BACKUPS = root / "backups"
    manager.RELEASE_STATE = root / ".releases.json"
    state = root / "state"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = "router-orchestrator-hub:" + manager.git_sha()[:12]
    healthy = "router-orchestrator-hub:" + project + "-next"
    broken = "router-orchestrator-hub:" + project + "-broken"
    # Do not reuse or later remove a pre-existing image.
    migrating = "router-orchestrator-hub:" + project + "-migration"
    images = [base, healthy, broken, migrating]
    for image in images:
        assert manager.call("docker", "image", "inspect", image, check=False).returncode != 0
    config = {"RO_IMAGE": base, "RO_STATE_DIR": str(state), "RO_PORT": str(port),
              "RO_PUBLIC_URL": "https://hub.example.invalid", "RO_XRAY_BACKEND": "external_readonly"}
    assert manager.compose("config", "--services", config=config).stdout.split() == ["hub"]
    try:
        cli("install", "--non-interactive", "--public-url", config["RO_PUBLIC_URL"],
            "--port", port, "--state-dir", state)
        base_id = manager.image_id(base)
        assert_api(port)
        with database(state) as db:
            db.execute("CREATE TABLE ro_ci_marker (value TEXT NOT NULL)")
            db.execute("INSERT INTO ro_ci_marker VALUES ('synthetic-row')")
            db.commit()
        (state / "ci-fixtures").mkdir()
        (state / "ci-fixtures" / "aux.txt").write_text("synthetic-file\n")
        manager.compose("restart", "hub")
        manager.verified_hub(config, base_id)
        assert_api(port)
        assert_data(state)

        manager.compose_stop()
        cli("backup")
        archives = list(manager.BACKUPS.glob("*.tar.gz"))
        assert len(archives) == 1
        restored = root / "restored"
        cli("restore", archives[0], "--destination", restored)
        assert_data(restored)
        manager.compose_up(base_id)

        fixture_image(root, base, healthy, f'LABEL org.router-orchestrator.ci="{project}"')
        with patch.object(manager, "build_image", return_value=healthy):
            cli("upgrade", "--acknowledge-schema-risk")
        assert manager.image_id(healthy) != base_id
        manager.verified_hub(manager.settings(), manager.image_id(healthy))
        assert_api(port)
        assert_data(state)
        cli("rollback", "--confirm")
        assert manager.settings() == config
        manager.verified_hub(config, base_id)

        fixture_image(root, base, broken, 'CMD ["python", "-c", "import time; time.sleep(600)"]')
        with patch.object(manager, "build_image", return_value=broken):
            cli("upgrade", "--acknowledge-schema-risk", expected=2)
        assert manager.read_transaction()["phase"] == "recovered"
        assert manager.settings() == config
        manager.verified_hub(config, base_id)
        assert_data(state)
        interrupt_after_env(healthy)
        assert manager.settings() == config
        manager.verified_hub(config, base_id)
        assert_api(port)
        assert_data(state)
        migration = (
            "import sqlite3,time; "
            "db=sqlite3.connect('/var/lib/owrt-remote/hub.db'); "
            "db.execute('ALTER TABLE ro_ci_marker ADD COLUMN migrated TEXT'); "
            "db.execute(\"UPDATE ro_ci_marker SET migrated='keep-latest'\"); "
            "db.commit(); db.close(); time.sleep(600)"
        )
        fixture_image(root, base, migrating, "CMD " + json.dumps(["python", "-c", migration]))
        with patch.object(manager, "build_image", return_value=migrating):
            cli("upgrade", "--acknowledge-schema-risk", expected=2)
        assert manager.pending_transaction()["phase"] == "switching"
        assert manager.docker_hub_states() == ["exited"]
        assert manager.settings()["RO_IMAGE"] == migrating
        with database(state) as db:
            assert db.execute("SELECT * FROM ro_ci_marker").fetchall() == [
                ("synthetic-row", "keep-latest")]
    finally:
        cleanup(project, config, images)


def interrupt(signum, _frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    assert os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
    assert os.geteuid() == 0 and "DOCKER_HOST" not in os.environ and "DOCKER_CONTEXT" not in os.environ
    signal.signal(signal.SIGTERM, interrupt)
    with tempfile.TemporaryDirectory(prefix="ro-ci-", dir="/tmp") as directory:
        run(Path(directory))
