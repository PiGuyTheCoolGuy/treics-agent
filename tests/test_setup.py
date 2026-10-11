from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def test_firewall_blocks_private_destinations_and_only_flushes_owned_chains(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            log = directory / "iptables.jsonl"
            stub = "#!/usr/bin/env python3\nimport json,sys\n" + \
                   f"with open({str(log)!r},'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n" + \
                   "raise SystemExit(1 if '-C' in sys.argv or '-N' in sys.argv else 0)\n"
            for name in ("iptables", "ip6tables"):
                path = directory / name
                path.write_text(stub)
                path.chmod(0o755)
            subprocess.run(["bash", str(ROOT / "scripts/host-firewall.sh")],
                           env={**os.environ, "PATH": str(directory) + ":" + os.environ["PATH"]},
                           check=True, capture_output=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            flushes = [c[c.index("-F") + 1] for c in commands if "-F" in c]
            self.assertEqual(set(flushes), {"TREIC_AGENT_IN", "TREIC_AGENT_FWD"})
            rejects = [ipaddress.ip_network(c[c.index("-d") + 1]) for c in commands
                       if "TREIC_AGENT_FWD" in c and "-d" in c and "REJECT" in c]
            for private in ("10.2.3.4", "172.16.4.5", "192.168.1.1", "100.64.2.3", "169.254.169.254", "127.0.0.1"):
                self.assertTrue(any(ipaddress.ip_address(private) in network for network in rejects))
            for public in ("1.1.1.1", "8.8.8.8"):
                self.assertFalse(any(ipaddress.ip_address(public) in network for network in rejects))
            self.assertIn(["-w", "-I", "FORWARD", "1", "-i", "virbr-treic", "-j", "TREIC_AGENT_FWD"], commands)
            accepts = [c for c in commands if "TREIC_AGENT_IN" in c and "--dport" in c and "ACCEPT" in c]
            self.assertEqual({c[c.index("--dport") + 1] for c in accepts}, {"53", "67", "11435"})

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd tools unavailable")
    def test_guest_units_parse_with_spaces_in_checkout_path(self):
        script = (ROOT / "scripts/install-guest.sh").read_text()
        generator = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            repo = directory / "repo with spaces"
            (repo / ".venv/bin").mkdir(parents=True)
            executable = repo / ".venv/bin/treics-agent"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            units = directory / ".config/systemd/user"
            units.mkdir(parents=True)
            with patch("pathlib.Path.home", return_value=directory), patch("sys.argv", ["generator", str(repo)]):
                exec(compile(generator, "guest-unit-generator", "exec"), {})
            result = subprocess.run(["systemd-analyze", "verify", *map(str, units.glob("*.service"))],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
