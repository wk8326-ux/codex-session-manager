from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DesktopDistributionContractTests(unittest.TestCase):
    def test_tauri_shell_keeps_remote_ipc_disabled(self) -> None:
        config = json.loads(
            (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(config["identifier"], "io.github.wk8326.local-project-console")
        self.assertEqual(config["version"], "0.1.0")
        self.assertEqual(config["bundle"]["targets"], ["nsis"])
        self.assertEqual(config["bundle"]["windows"]["nsis"]["installMode"], "currentUser")
        self.assertNotIn("dangerousRemoteDomainIpcAccess", config["app"]["security"])

        cargo = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('custom-protocol = ["tauri/custom-protocol"]', cargo)

    def test_packaged_backend_excludes_local_state(self) -> None:
        spec = (ROOT / "packaging" / "lpc-service.spec").read_text(encoding="utf-8")

        for resource in ("index.html", "watchdog.html", "remote.html", "assets"):
            self.assertIn(resource, spec)
        for private_path in ("projects.json", "watchdog.db", ".runtime"):
            self.assertNotIn(f'project_root / "{private_path}"', spec)

    def test_release_build_produces_installer_portable_zip_and_checksums(self) -> None:
        script = (ROOT / "scripts" / "build-desktop.ps1").read_text(
            encoding="utf-8"
        )

        for artifact in (
            "LocalProjectConsole-Setup-x64.exe",
            "LocalProjectConsole-Portable-x64.zip",
            "portable.flag",
            "SHA256",
        ):
            self.assertIn(artifact, script)

    def test_desktop_shell_exposes_required_tray_actions(self) -> None:
        source = (
            ROOT / "desktop" / "src-tauri" / "src" / "lib.rs"
        ).read_text(encoding="utf-8")

        for label in (
            "打开项目控制台",
            "会话监控",
            "远程会话",
            "重启后台服务",
            "停止后台服务",
            "退出桌面应用",
            "repair_runtime",
            "EXTERNAL_NAVIGATION_SHIM",
        ):
            self.assertIn(label, source)

    def test_startup_page_exposes_delayed_recovery_actions(self) -> None:
        markup = (ROOT / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "desktop" / "ui" / "startup.js").read_text(encoding="utf-8")

        self.assertIn('id="repair-button"', markup)
        self.assertIn("repair_runtime", script)
        self.assertIn("5000", script)


if __name__ == "__main__":
    unittest.main()
