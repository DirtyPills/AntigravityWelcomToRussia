import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("agy_net", Path(__file__).parents[1] / "agy_net.py")
agy = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(agy)


class ConfigTests(unittest.TestCase):
    def config(self, endpoint="198.51.100.7:51820"):
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(f"[Interface]\nPrivateKey = secret\nAddress = 10.0.0.2/32\nDNS = 1.1.1.1\n[Peer]\nPublicKey = public\nEndpoint = {endpoint}\nAllowedIPs = 0.0.0.0/0\n")
        handle.close(); os.chmod(handle.name, 0o600)
        return Path(handle.name)

    def test_numeric_endpoint_is_accepted(self):
        path = self.config(); self.addCleanup(path.unlink)
        self.assertEqual(agy.validate_conf(path), ("198.51.100.7", 51820))

    def test_hostname_endpoint_is_rejected_to_prevent_bootstrap_leak(self):
        path = self.config("vpn.example.test:51820"); self.addCleanup(path.unlink)
        with self.assertRaises(agy.AgyError): agy.validate_conf(path)

    def test_world_readable_secret_is_rejected(self):
        path = self.config(); self.addCleanup(path.unlink); os.chmod(path, 0o644)
        with self.assertRaises(agy.AgyError): agy.validate_conf(path)

    def test_awg_config_keeps_mtu_value_available(self):
        path = self.config(); self.addCleanup(path.unlink)
        self.assertIsNone(agy.interface_value(path, "MTU"))

    def test_host_rules_are_scoped_to_amnezia_transport(self):
        rules = agy.host_nft_script()
        self.assertIn('oifname "amn0"', rules)
        self.assertIn('iifname "agy-host0" drop', rules)
        self.assertNotIn("other-vpn", rules)
        self.assertNotIn("wifi0", rules)

    def test_custom_transport_rules_are_scoped_to_the_selected_tun_interface(self):
        rules = agy.host_nft_script("vless0")
        self.assertIn('oifname "vless0"', rules)
        self.assertNotIn('oifname "amn0"', rules)
        self.assertIn('iifname "agy-host0" drop', rules)

    def test_managed_awg_rules_permit_only_the_numeric_udp_endpoint_over_the_host_transport(self):
        host_rules = agy.host_nft_script("amn0", "198.51.100.7", 51820)
        namespace_rules = agy.nft_script("198.51.100.7", 51820)
        self.assertIn('ip daddr 198.51.100.7 udp dport 51820', host_rules)
        self.assertIn('oifname "awg0" accept', namespace_rules)
        self.assertIn('oifname "agy-net0" ip daddr 198.51.100.7 udp dport 51820 accept', namespace_rules)
        self.assertNotIn('iifname "agy-host0" oifname "amn0" accept', host_rules)

    def test_awg_runtime_config_removes_only_interface_dns(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "awg0.conf"
            config.write_text("[Interface]\nPrivateKey = secret\nDNS = 1.1.1.1\nAddress = 10.0.0.2/32\n[Peer]\nDNS = not-a-resolver\nPublicKey = public\n")
            self.assertNotIn("DNS = 1.1.1.1", agy.awg_config_without_dns(config))
            self.assertIn("DNS = not-a-resolver", agy.awg_config_without_dns(config))

    def test_tunnel_mode_is_strictly_validated(self):
        self.assertEqual(agy.validate_tunnel_mode("awg"), "awg")
        self.assertEqual(agy.validate_tunnel_mode("reuse"), "reuse")
        with self.assertRaises(agy.AgyError):
            agy.validate_tunnel_mode("other")

    def test_awg_mode_uses_the_host_default_route_for_bootstrap(self):
        runner = mock.Mock(dry_run=False)
        runner.run.return_value = subprocess.CompletedProcess([], 0, "default via 192.0.2.1 dev enp6s0\n", "")
        with mock.patch.object(agy, "ns_exists", return_value=False):
            self.assertEqual(agy.selected_transport(runner, None, "awg"), "enp6s0")

    def test_transport_interface_name_is_strictly_validated(self):
        self.assertEqual(agy.validate_transport_interface("tun0"), "tun0")
        with self.assertRaises(agy.AgyError):
            agy.validate_transport_interface('tun0"; flush ruleset #')

    def test_explicit_dns_override_does_not_depend_on_awg_config(self):
        self.assertEqual(agy.parse_dns_override("1.1.1.1, 1.0.0.1"), ["1.1.1.1", "1.0.0.1"])
        with self.assertRaises(agy.AgyError):
            agy.parse_dns_override("not-a-dns-server")

    def test_root_only_runtime_transport_is_reported_without_traceback(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            with self.assertRaisesRegex(agy.AgyError, "requires root"):
                agy.recorded_transport()

    def test_dns_servers_are_validated(self):
        path = self.config(); self.addCleanup(path.unlink)
        self.assertEqual(agy.dns_servers(path), ["1.1.1.1"])

    def test_split_dns_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "awg0.conf"
            config.write_text("[Interface]\nPrivateKey = secret\nAddress = 10.0.0.2/32\nDNS = 1.1.1.1\n[Peer]\nPublicKey = public\nEndpoint = 198.51.100.7:51820\nAllowedIPs = 0.0.0.0/0\n")
            os.chmod(config, 0o600)
            (Path(directory) / "dns.yaml").write_text("dns:\n  mode: split\n  default:\n    - 1.1.1.1\n  routes:\n    example.test:\n      - 10.10.10.53\n")
            self.assertEqual(agy.dns_settings(config), ("split", ["1.1.1.1"], {"example.test": ["10.10.10.53"]}))

    def test_log_redaction_removes_credentials_and_query(self):
        text = "PrivateKey = secret Authorization=BearerToken https://example.test/path?access_token=secret"
        redacted = agy.redact(text)
        self.assertNotIn("BearerToken", redacted)
        self.assertNotIn("access_token=secret", redacted)
        self.assertIn("<redacted>", redacted)

    def test_desktop_service_is_bound_to_agy_net_and_uses_private_resolver(self):
        unit = (Path(__file__).parents[1] / "systemd" / "agy-net-antigravity@.service").read_text()
        self.assertIn("BindsTo=agy-net.service", unit)
        self.assertIn("NetworkNamespacePath=/run/netns/agy-net", unit)
        self.assertIn("BindReadOnlyPaths=/run/agy-net/resolv.conf:/etc/resolv.conf", unit)
        self.assertIn("ExecStartPre=/usr/local/lib/agy-net/desktop-exec --user %i --validate", unit)

    def test_desktop_launcher_uses_a_generic_browser_handler_outside_xfce_exo_open(self):
        environment = {"HOME": "/home/test"}
        with mock.patch.object(agy.shutil, "which", side_effect=lambda name: "/usr/bin/firefox" if name == "firefox" else None):
            agy.browser_environment(environment)
        self.assertEqual(environment["XDG_CURRENT_DESKTOP"], "X-Generic")
        self.assertEqual(environment["BROWSER"], "/usr/bin/firefox")
        helper = (Path(__file__).parents[1] / "desktop_exec.py").read_text()
        self.assertIn('"BROWSER"', helper)
        self.assertIn('"XDG_CURRENT_DESKTOP"', helper)

    def test_desktop_helper_refuses_root_service_user(self):
        helper = Path(__file__).parents[1] / "desktop_exec.py"
        result = subprocess.run(["python3", str(helper), "--user", "root", "--validate"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root", result.stderr)


if __name__ == "__main__": unittest.main()
