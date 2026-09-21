from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remote.setup import RelaySetupApi, RelaySetupService


ROOT = Path(__file__).resolve().parents[1]


def encoded_bundle(**overrides: object) -> str:
    payload = {
        "format": "lpc-frp-v1",
        "serverAddr": "203.0.113.10",
        "serverPort": 7000,
        "remotePort": 18766,
        "frpVersion": "0.61.1",
        "publicUrl": "https://console.example.com",
        "token": "a" * 64,
        **overrides,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


class RelaySetupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RelaySetupService(ROOT)

    def test_server_plan_contains_only_public_deployment_values(self) -> None:
        plan = self.service.server_plan(
            {
                "vpsHost": "203.0.113.10",
                "sshUser": "ubuntu",
                "domain": "console.example.com",
                "frpPort": 7000,
                "remotePort": 18766,
                "frpVersion": "0.61.1",
            }
        )

        self.assertEqual(plan["publicUrl"], "https://console.example.com")
        self.assertIn("/tmp/csm-setup-relay.sh", plan["serverCommand"])
        self.assertIn("--domain console.example.com", plan["serverCommand"])
        self.assertEqual(plan["scriptSource"], "bundled")
        self.assertNotIn("raw.githubusercontent.com", plan["serverCommand"])
        self.assertNotIn("token", plan["serverCommand"].lower())
        self.assertNotIn("password", plan["serverCommand"].lower())

    def test_server_plan_rejects_credentials_and_unknown_fields(self) -> None:
        api = RelaySetupApi(self.service)
        response = api.dispatch(
            "POST",
            "/api/relay-setup/server-plan",
            {
                "vpsHost": "root:secret@example.com",
                "domain": "console.example.com",
                "privateKey": "secret",
            },
        )

        self.assertEqual(response.status, 400)

    def test_bundle_summary_never_returns_the_frp_token(self) -> None:
        secret = "super-secret-frp-token-1234567890abcdef"
        summary = self.service.inspect_bundle({"bundle": encoded_bundle(token=secret)})

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["serverAddr"], "203.0.113.10")
        self.assertNotIn("token", summary)
        self.assertNotIn(secret, json.dumps(summary))

    def test_status_exposes_script_and_runtime_state_without_credentials(self) -> None:
        service = RelaySetupService(
            ROOT,
            tunnel_status_provider=lambda: {
                "provider": "frp",
                "configured": True,
                "running": True,
                "state": "running",
                "pid": 1234,
                "startedAt": "2026-08-06T00:00:00Z",
                "detail": "",
            },
        )

        status = service.status()

        self.assertTrue(status["clientScriptReady"])
        self.assertIn("setup-remote-client.ps1", status["clientCommand"])
        self.assertEqual(status["tunnel"]["state"], "running")
        self.assertNotIn("token", json.dumps(status).lower())

    def test_tunnel_start_uses_the_console_owned_manager(self) -> None:
        start = MagicMock(return_value=True)
        service = RelaySetupService(
            ROOT,
            tunnel_start_provider=start,
            tunnel_status_provider=lambda: {
                "provider": "frp",
                "configured": True,
                "running": True,
                "state": "running",
            },
        )

        result = service.start_tunnel()

        start.assert_called_once_with()
        self.assertTrue(result["started"])
        self.assertTrue(result["tunnel"]["running"])

    def test_dns_check_compares_resolved_addresses(self) -> None:
        with patch(
            "remote.setup._resolve",
            side_effect=[["203.0.113.10"], ["203.0.113.10"]],
        ):
            result = self.service.check_dns(
                {"domain": "console.example.com", "vpsHost": "relay.example.com"}
            )

        self.assertTrue(result["matched"])
        self.assertEqual(result["state"], "matched")

    def test_public_verification_reports_http_reachability(self) -> None:
        response = MagicMock()
        response.getcode.return_value = 200
        response.__enter__.return_value = response
        with patch("remote.setup.urlopen", return_value=response):
            result = self.service.verify_public_access(
                {"publicUrl": "https://console.example.com"}
            )

        self.assertTrue(result["reachable"])
        self.assertEqual(result["status"], 200)


class RelaySetupScriptContractTests(unittest.TestCase):
    def test_server_installer_is_idempotent_and_verifies_the_download(self) -> None:
        script = (ROOT / "scripts" / "setup-relay-server.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("set -Eeuo pipefail", script)
        self.assertIn("backup_if_present", script)
        self.assertIn("sha256sum", script)
        self.assertIn("systemctl enable --now lpc-frps.service", script)
        self.assertIn("LPC_CONFIG_BUNDLE=", script)

    def test_relay_nginx_disables_buffering_for_the_long_poll_feed(self) -> None:
        script = (ROOT / "scripts" / "setup-relay-server.sh").read_text(
            encoding="utf-8"
        )

        # The event feed is a long poll. Buffering it delays every push and
        # the default 1m body limit rejects screenshot uploads with 413.
        self.assertIn("location /api/remote/events", script)
        self.assertIn("proxy_buffering off;", script)
        self.assertIn("client_max_body_size 8m;", script)
        self.assertIn("proxy_set_header Connection \"\";", script)

    def test_windows_installer_dry_run_decodes_without_writing_the_token(self) -> None:
        bundle = encoded_bundle()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "setup-remote-client.ps1"),
                    "-Bundle",
                    bundle,
                    "-ProjectRoot",
                    directory,
                    "-DryRun",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry run passed", result.stdout)
            self.assertNotIn("a" * 64, result.stdout)
            self.assertFalse((Path(directory) / ".runtime").exists())

    def test_frp_client_configs_ship_pool_and_health_check(self) -> None:
        # "work connection pool is full, discarding" fired 633 times in one
        # second with the default poolCount of 1, and the proxy kept
        # forwarding connections while 127.0.0.1:8766 was down. Both the
        # bundled example and the generated client config must carry the
        # tuning so a fresh install cannot reproduce either failure.
        example = (ROOT / "scripts" / "frpc.example.toml").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "setup-remote-client.ps1").read_text(
            encoding="utf-8"
        )

        for source in (example, installer):
            self.assertIn("poolCount = 8", source)
            self.assertIn("heartbeatInterval = 30", source)
            self.assertIn("heartbeatTimeout = 90", source)
            self.assertIn("[proxies.healthCheck]", source)
            self.assertIn('type = "tcp"', source)
            self.assertIn("maxFailed = 2", source)
            self.assertIn("intervalSeconds = 3", source)


class RelaySetupUiContractTests(unittest.TestCase):
    def test_remote_device_view_links_to_the_local_only_setup_wizard(self) -> None:
        remote_html = (ROOT / "remote.html").read_text(encoding="utf-8")
        navigation = (ROOT / "assets" / "session-manager-nav.js").read_text(encoding="utf-8")
        server = (ROOT / "app.py").read_text(encoding="utf-8")
        remote_handler = server[server.index("class RemoteHandler"):]

        self.assertIn('id="open-remote-setup" href="/remote-setup"', remote_html)
        self.assertIn("配置或迁移远程访问", remote_html)
        self.assertNotIn("remote-setup", navigation)
        self.assertNotIn('"/remote-setup"', remote_handler)

    def test_wizard_restores_only_contiguous_steps_and_never_persists_bundle(self) -> None:
        script = (ROOT / "remote-setup.js").read_text(encoding="utf-8")
        persist = script[script.index("function persistState"):script.index("function showToast")]

        self.assertIn("normalizeCompletedSteps", script)
        self.assertIn("nextAvailableStep", script)
        self.assertNotIn("activeBundle", persist)
        self.assertIn("renderBundleAvailability", script)
        self.assertIn("进度已保存在本机", script)

    def test_wizard_assets_are_versioned_and_not_cached(self) -> None:
        html = (ROOT / "remote-setup.html").read_text(encoding="utf-8")
        server = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn('/remote-setup.css?v=2', html)
        self.assertIn('/remote-setup.js?v=2', html)
        admin_handler = server[server.index("class AdminHandler"):server.index("class RemoteHandler")]
        self.assertIn('"/remote-setup"', admin_handler)
        self.assertIn('"/remote-setup.css"', admin_handler)
        self.assertIn('"/remote-setup.js"', admin_handler)

    def test_wizard_mobile_controls_and_focus_target_are_stable(self) -> None:
        html = (ROOT / "remote-setup.html").read_text(encoding="utf-8")
        stylesheet = (ROOT / "remote-setup.css").read_text(encoding="utf-8")

        self.assertIn('id="setup-stage" tabindex="-1"', html)
        self.assertIn('class="button-label"', html)
        self.assertIn("@media (max-width: 560px)", stylesheet)
        self.assertIn("input, textarea { font-size: 16px; }", stylesheet)
        self.assertIn("min-height: 44px", stylesheet)
        self.assertIn(".rail-foot .text-button { min-height: 44px;", stylesheet)


if __name__ == "__main__":
    unittest.main()
