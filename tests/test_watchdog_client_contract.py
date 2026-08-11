import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchdogClientContractTests(unittest.TestCase):
    def read_html(self) -> str:
        return (ROOT / "watchdog.html").read_text(encoding="utf-8")

    def test_client_uses_watchdog_api_plus_read_only_shell_summary(self) -> None:
        html = self.read_html()
        api_paths = re.findall(r'["\'`](/api/[^"\'`?\s]*)', html)
        shell_summary = "/api/shell/project-summary"

        self.assertTrue(api_paths, "watchdog client must declare its API endpoints")
        self.assertTrue(
            all(
                path.startswith("/api/watchdog/") or path == shell_summary
                for path in api_paths
            ),
            f"unexpected cross-workspace API path found: {api_paths}",
        )
        self.assertNotIn("/api/projects", html)
        self.assertIn(shell_summary, html)
        for endpoint in (
            "/api/watchdog/status",
            "/api/watchdog/settings",
            "/api/watchdog/sessions",
            "/api/watchdog/channels",
            "/api/watchdog/recovery-rules",
            "/api/watchdog/runs",
            "/api/watchdog/local-codex-sessions",
        ):
            self.assertIn(endpoint, html)

    def test_session_directory_exposes_crud_enable_and_manual_check_actions(self) -> None:
        html = self.read_html()

        for marker in (
            'id="session-form"',
            'data-session-action="edit"',
            'data-session-action="delete"',
            'data-session-action="toggle"',
            'data-session-action="check"',
            "/api/watchdog/sessions",
            "/check",
        ):
            self.assertIn(marker, html)
        self.assertRegex(
            html,
            r'<button[^>]+(?:id|data-action)="add-session"',
        )

    def test_channel_directory_exposes_crud_enable_and_probe_actions(self) -> None:
        html = self.read_html()

        for marker in (
            'id="channel-form"',
            'data-channel-action="edit"',
            'data-channel-action="delete"',
            'data-channel-action="toggle"',
            'data-channel-action="probe"',
            "/api/watchdog/channels",
            "/probe",
            'name="apiKey"',
            "apiKeyMasked",
        ):
            self.assertIn(marker, html)
        self.assertRegex(
            html,
            r'<button[^>]+(?:id|data-action)="add-channel"',
        )

    def test_error_type_directory_exposes_text_rule_crud(self) -> None:
        html = self.read_html()

        for marker in (
            'id="tab-rules"',
            'id="rule-form"',
            'name="pattern"',
            'data-rule-action="edit"',
            'data-rule-action="delete"',
            'data-rule-action="toggle"',
            "/api/watchdog/recovery-rules",
            "系统内置",
            "文本包含",
        ):
            self.assertIn(marker, html)
        self.assertRegex(
            html,
            r'<button[^>]+(?:id|data-action)="add-rule"',
        )

    def test_global_resume_setting_requires_an_explicit_confirmation_surface(self) -> None:
        html = self.read_html()

        self.assertIn('id="resume-control"', html)
        self.assertRegex(html, r'id="resume-(?:confirm-)?dialog"')
        self.assertRegex(html, r'id="(?:confirm-resume|confirm-resume-actions)"')
        self.assertIn("resumeActionsEnabled", html)
        self.assertIn("专用测试会话", html)
        self.assertIn("/api/watchdog/settings", html)

    def test_unattended_approval_is_an_explicit_per_session_setting(self) -> None:
        html = self.read_html()

        self.assertIn('id="session-unattended-approvals"', html)
        self.assertIn('name="unattendedApprovalsEnabled"', html)
        self.assertIn("命令执行和文件修改", html)
        self.assertIn("权限扩展和模型提问仍会停下", html)
        self.assertIn("unattendedApprovalsEnabled", html)

    def test_resume_dispatch_mode_exposes_desktop_realtime_bridge(self) -> None:
        html = self.read_html()

        self.assertIn('id="resume-dispatch-mode"', html)
        self.assertIn('value="desktop_bridge"', html)
        self.assertIn('value="direct_app_server"', html)
        self.assertIn("本机 App Server · 推荐", html)
        self.assertIn("Codex Desktop · 兼容桥接", html)
        self.assertIn("本机恢复代理 · 直接续跑", html)
        self.assertIn("resumeDispatchMode", html)
        self.assertIn("desktopBridge", html)
        self.assertIn("resume_queued", html)

    def test_run_history_provides_all_supported_filters(self) -> None:
        html = self.read_html()

        self.assertRegex(html, r'id="runs?-filter(?:s|-form)"')
        for field in ("sessionId", "channelId", "decision", "from", "to"):
            self.assertIn(f'name="{field}"', html)
        for decision_label in ("静默", "已续跑", "续跑失败", "需要关注"):
            self.assertIn(decision_label, html)
        self.assertIn('value="resume_cancelled"', html)
        self.assertIn("续跑已取消", html)
        self.assertIn("/api/watchdog/runs", html)

    def test_local_threads_are_loaded_only_from_a_user_action(self) -> None:
        html = self.read_html()
        script = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL))

        self.assertRegex(
            html,
            r'<button[^>]+(?:id|data-action)="load-local-sessions"',
        )
        self.assertIn("/api/watchdog/local-codex-sessions", script)
        self.assertRegex(script, r"\bloadLocalSessions\b")
        self.assertRegex(
            script,
            r"(?s)(?:getElementById|querySelector)\([^)]*load-local-sessions[^)]*\).{0,500}addEventListener\(\s*['\"]click['\"]",
        )
        self.assertNotRegex(
            script,
            r"(?:DOMContentLoaded|window\.onload)[^;]{0,500}loadLocalSessions\s*\(",
        )

    def test_lightweight_watchdog_data_polls_every_fifteen_seconds(self) -> None:
        script = self.read_html()

        self.assertRegex(
            script,
            r"WATCHDOG_POLL_INTERVAL_MS\s*=\s*(?:15_000|15000)",
        )
        self.assertRegex(
            script,
            r"setInterval\([^;]*WATCHDOG_POLL_INTERVAL_MS[^;]*\)",
        )
        self.assertRegex(script, r"\bpollWatchdogOverview\b")
        self.assertNotRegex(script, r"setInterval\([^;]*(?:5_000|5000)[^;]*\)")

    def test_background_poll_refreshes_the_visible_watchdog_tab(self) -> None:
        script = self.read_html()

        self.assertRegex(script, r"\brefreshVisibleWatchdogData\b")
        self.assertRegex(script, r"(?s)tab-runs.{0,180}refreshRuns\(")
        self.assertRegex(script, r"(?s)tab-channels.{0,180}refreshChannels\(")
        self.assertRegex(script, r"(?s)tab-rules.{0,180}refreshRules\(")
        self.assertRegex(
            script,
            r"addEventListener\(\s*['\"]visibilitychange['\"]",
        )
        self.assertRegex(
            script,
            r"(?s)function activateTab\([^)]*\).{0,700}refreshVisibleWatchdogData\(",
        )

    def test_enabled_session_rows_show_live_next_check_countdown(self) -> None:
        html = self.read_html()

        self.assertIn("data-session-countdown", html)
        self.assertIn("updateSessionCountdowns", html)
        self.assertIn("nextCheckAt", html)
        self.assertIn("effectiveIntervalMinutes", html)
        self.assertIn("后检查", html)
        self.assertIn("已暂停", html)
        self.assertRegex(
            html,
            r"setInterval\(updateSessionCountdowns,\s*1000\)",
        )

    def test_mutations_prevent_duplicate_submissions_and_report_inline_errors(self) -> None:
        html = self.read_html()

        self.assertIn('aria-live="polite"', html)
        self.assertRegex(html, r"\bwithPendingAction\b")
        self.assertRegex(html, r"\.disabled\s*=\s*true")
        self.assertIn('id="session-form-error" role="alert"', html)
        self.assertIn('id="channel-form-error" role="alert"', html)
        self.assertIn('id="rule-form-error" role="alert"', html)
        self.assertIn("aria-describedby=", html)
        self.assertRegex(html, r"setAttribute\(\s*['\"]aria-busy['\"]")


if __name__ == "__main__":
    unittest.main()
