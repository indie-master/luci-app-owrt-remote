"""Prepare systemd files for one managed Stack under the deployment lock."""

from pathlib import Path
import re
import stat

import manager as m
from stack_runtime import protected
from unified_core_manager import HOST_STACK_ENV, configured_stack_env
from unified_xray_state import BACKEND

UNIT_DIRECTORY = Path("/etc/systemd/system")
OTHER_UNIT_DIRECTORIES = tuple(map(Path, (
    "/etc/systemd/system.control", "/run/systemd/system.control", "/run/systemd/transient",
    "/run/systemd/system", "/usr/local/lib/systemd/system", "/usr/lib/systemd/system",
)))
ADAPTER = "router-orchestrator-host-adapter.service"
WATCHDOG = "router-orchestrator-xray-watchdog.service"
TIMER = "router-orchestrator-xray-watchdog.timer"


def exists(path):
    return path.exists() or path.is_symlink()


def prepare(stack):
    m.require_root()
    m.require(m.inherited_lock(), "Host service preparation requires the deployment lock")
    m.require(stack.config["RO_XRAY_BACKEND"] == BACKEND, "Require the unified adapter backend")
    base = m.HUB_DEPLOY_ROOT
    m.require(protected(base).is_dir(), "Unsafe deployment directory")
    src = base / "src"
    m.require(re.fullmatch(r"/[A-Za-z0-9_./-]+", str(stack.path)) and
              re.fullmatch(r"/[A-Za-z0-9_./-]+", str(base)), "Unsafe systemd selector path")
    paths = [stack.path, stack.state, *(Path(stack.config[key]) for key in (
        "RO_XRAY_CONFIG_DIR", "RO_XRAY_ASSET_DIR", "RO_TLS_DIR"))]
    if stack.plan["nginx_conf"] is not None:
        paths.append(stack.plan["nginx_conf"])
    m.require(all(base in path.parents and not path.is_relative_to(src) and path.resolve() == path
                  for path in paths), "Managed Stack paths must be inside the deployment directory")
    m.require(stack.path.stat().st_nlink == 1, "Unsafe stack env hard link")
    for path in paths[2:5]:
        m.require(protected(path).is_dir(), "Expected a protected Xray or TLS directory")
    if stack.plan["nginx_conf"] is not None:
        m.require(protected(stack.plan["nginx_conf"]).is_file(), "Expected a protected nginx config file")
    stack.state_permissions(initializing=True)
    m.require(not stack.xray_hold(), "Xray maintenance HOLD is active")
    m.require(configured_stack_env() in (None, stack.path), "Conflicting host stack selectors")
    m.require(protected(UNIT_DIRECTORY).is_dir(), "Unsafe systemd unit directory")

    selector = base / "managed-stack.conf"
    environment = f"EnvironmentFile={selector}"
    required = f"Environment={HOST_STACK_ENV}=\n{environment}"
    files = {selector: f"{HOST_STACK_ENV}={stack.path}\n"}
    for module in ("host_adapter.py", "xray_watchdog.py"):
        m.require(protected(src / "docker" / module).is_file(), "Managed service entry point missing")
    for name in (ADAPTER, WATCHDOG, TIMER):
        template = protected(src / "deploy" / "systemd" / name).read_text()
        # Templates also serve legacy deployments. The unified units require the same selector.
        template = template.replace("/opt/router-orchestrator", str(base))
        if name == ADAPTER:
            template = template.replace("[Service]\n", "[Service]\n" + required + "\n")
            template = template.replace(" --peer-uid 23088 --allow-xray-restart --allow-xray-apply", "")
            template = re.sub(r"(?m)^ReadWritePaths=.*$",
                              f"ReadWritePaths={base} /run/router-orchestrator\nReadOnlyPaths={src}", template)
        elif name == WATCHDOG:
            template = template.replace(f"EnvironmentFile=-{selector}", required)
        m.require(name == TIMER or (
            [line for line in template.splitlines() if line.startswith("EnvironmentFile=")] == [environment]
            and not any(flag in template for flag in ("--stack-env", "--allow-xray-"))),
            "Conflicting selectors in service template")
        files[UNIT_DIRECTORY / name] = template
        stem, kind = name.rsplit(".", 1)
        prefixes = stem.split("-")
        drop_ins = [f"{name}.d", f"{kind}.d", *(f'{"-".join(prefixes[:i])}-.{kind}.d'
                    for i in range(1, len(prefixes)))]
        for directory in (UNIT_DIRECTORY, *OTHER_UNIT_DIRECTORIES):
            m.require(not any(exists(directory / drop_in) for drop_in in drop_ins) and
                      (directory == UNIT_DIRECTORY or not exists(directory / name)),
                      "Conflicting systemd unit or drop-in")

    # Check the complete set before writing anything. Publish the selector first so
    # a retry after a partial write uses exactly the same Stack.
    missing = {}
    for path, contents in files.items():
        if exists(path):
            info = protected(path).stat()
            m.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                      (path != selector or stat.S_IMODE(info.st_mode) == 0o600) and
                      info.st_size == len(contents.encode()) and path.read_text() == contents,
                      "Conflicting managed Stack selector or unit")
        else:
            missing[path] = contents
    for path, contents in missing.items():
        m.durable_write(path, contents, exclusive=True)
