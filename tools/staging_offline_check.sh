#!/bin/sh
# Low-resource synthetic staging checks; never launches a service or container.
set -eu
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info[:3] == (3, 11, 2), "requires Python 3.11.2"'
python3 -m unittest discover -s tests -p 'test_belt_gateway_channels.py' -v
python3 -m unittest discover -s tests -p 'test_docker_backup_recovery.py' -v
python3 -m unittest discover -s tests -p 'test_orchestrator_docker_runtime.py' -v
python3 -m unittest discover -s tests -p 'test_orchestrator_edge_plan.py' -v
python3 -m unittest discover -s tests -p 'test_orchestrator_stack_config.py' -v
python3 -m unittest discover -s tests -p 'test_router_fresh_install.py' -v
python3 -m unittest discover -s tests -p 'test_belt_hub_host_export_hotfix.py' -v
python3 -m unittest discover -s tests -p 'test_belt_fleet_config_audit.py' -v
sh -n install.sh
sh -n tools/router_fresh_install.sh
sh -n docker/install.sh
