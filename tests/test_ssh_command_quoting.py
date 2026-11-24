"""Exercise the actual router SSH script runner without network access.

OpenSSH joins remote command arguments into a string evaluated by the remote
shell. Test that transport options cannot inject shell substitutions.
"""
import ast
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "vps" / "owrt-remote-hub.py"


class SSHRemoteArgumentsTest(unittest.TestCase):
    def test_unsafe_router_transport_values_are_shell_quoted(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        handler = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "Handler")
        method = next(node for node in handler.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.name == "run_router_ssh_script")
        # Compile the production method, not a reimplementation.
        class_node = ast.ClassDef(
            name="Runner", bases=[], keywords=[],
            body=[method], decorator_list=[])
        module = ast.fix_missing_locations(
            ast.Module(body=[class_node], type_ignores=[]))
        namespace = {"shlex": shlex, "subprocess": subprocess}
        exec(compile(module, str(SOURCE), "exec"), namespace)
        runner = namespace["Runner"]()
        runner.ssh_exec_args = lambda port, allow_password: ({}, ["ssh", "-p", str(port), "root@127.0.0.1"])
        runner.prepare_ssh_askpass = lambda env, password: None
        attacks = ["foo.example;touch /tmp/pwn", "$(id)", "has spaces",
                   "quote'break", "abc\nexit 1"]
        fake = subprocess.CompletedProcess([], 0, "PASS", "")
        with patch.object(subprocess, "run", return_value=fake) as mock_run:
            result = runner.run_router_ssh_script(
                {"ssh_entry_port": 19080}, "printf 'ok'\n",
                script_args=attacks
            )
        self.assertEqual(result, "PASS")
        argv = mock_run.call_args.args[0]
        self.assertEqual(len(argv), 5)
        self.assertEqual(shlex.split(argv[-1]), ["sh", "-s", "--", *attacks])
        self.assertNotIn(" ;touch ", argv[-1])
        self.assertIn("'$(id)'", argv[-1])

if __name__ == "__main__":
    unittest.main()
