from __future__ import annotations

import time
import unittest

from remote.approvals import (
    InvalidApprovalDecision,
    RemoteApprovalBroker,
)


THREAD_ID = "00000000-0000-4000-8000-000000000001"


class RemoteApprovalBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.audit: list[dict] = []
        self.decisions: list[str] = []
        self.broker = RemoteApprovalBroker(
            lambda: {THREAD_ID},
            timeout_seconds=2,
            publish_event=lambda method, params: self.events.append((method, params)),
            record_audit=self.audit.append,
        )

    def tearDown(self) -> None:
        self.broker.close()

    def test_synced_command_waits_for_an_explicit_remote_decision(self) -> None:
        queued = self.broker.offer(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-1",
                "command": ["npm", "test"],
                "cwd": "D:/workspace",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
            self.decisions.append,
        )

        self.assertTrue(queued)
        pending = self.broker.list_pending()[0]
        self.assertEqual(pending["summary"], "npm test")
        self.assertEqual(
            pending["availableDecisions"],
            ["accept", "acceptForSession", "decline"],
        )
        self.assertEqual(self.decisions, [])

        resolved = self.broker.resolve(
            pending["id"], "accept", {"id": "phone-1", "name": "My phone"}
        )

        self.assertEqual(self.decisions, ["accept"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.broker.list_pending(), [])
        self.assertEqual(self.audit[0]["actorDeviceName"], "My phone")
        self.assertEqual(self.audit[0]["decision"], "accept")
        self.assertEqual(
            [method for method, _params in self.events],
            ["remote/approvalRequested", "remote/approvalResolved"],
        )

    def test_unavailable_or_unsynced_decisions_fail_closed(self) -> None:
        self.assertFalse(
            self.broker.offer(
                "item/fileChange/requestApproval",
                {"threadId": "not-synced"},
                self.decisions.append,
            )
        )
        self.broker.offer(
            "item/permissions/requestApproval",
            {
                "threadId": THREAD_ID,
                "permissions": {"network": {"enabled": True}},
            },
            self.decisions.append,
        )
        pending = self.broker.list_pending()[0]
        self.assertEqual(pending["availableDecisions"], ["accept", "decline"])
        with self.assertRaises(InvalidApprovalDecision):
            self.broker.resolve(
                pending["id"],
                "acceptForSession",
                {"id": "phone-1", "name": "My phone"},
            )

    def test_allow_turn_auto_accepts_later_requests_for_the_same_turn_only(self) -> None:
        first_decisions: list[str] = []
        second_decisions: list[str] = []
        other_turn_decisions: list[str] = []
        actor = {"id": "phone-1", "name": "My phone"}
        self.broker.offer(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-1",
                "command": "npm test",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
            first_decisions.append,
        )
        pending = self.broker.list_pending()[0]

        resolved = self.broker.allow_turn(pending["id"], actor)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(first_decisions, ["acceptForSession"])

        self.assertTrue(
            self.broker.offer(
                "item/permissions/requestApproval",
                {"threadId": THREAD_ID, "turnId": "turn-1"},
                second_decisions.append,
            )
        )
        self.assertEqual(second_decisions, ["accept"])
        self.assertEqual(self.broker.list_pending(), [])
        self.assertEqual(self.audit[-1]["outcome"], "auto_approved")

        self.broker.offer(
            "item/fileChange/requestApproval",
            {"threadId": THREAD_ID, "turnId": "turn-2"},
            other_turn_decisions.append,
        )
        self.assertEqual(other_turn_decisions, [])
        self.assertEqual(len(self.broker.list_pending()), 1)

    def test_timeout_declines_without_blocking_the_app_server_reader(self) -> None:
        broker = RemoteApprovalBroker(
            lambda: {THREAD_ID},
            timeout_seconds=0.03,
            record_audit=self.audit.append,
        )
        broker.offer(
            "item/fileChange/requestApproval",
            {"threadId": THREAD_ID, "reason": "Update config"},
            self.decisions.append,
        )
        deadline = time.monotonic() + 1
        while not self.decisions and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(self.decisions, ["decline"])
        self.assertEqual(self.audit[-1]["outcome"], "expired")
        self.assertEqual(broker.list_pending(), [])
        broker.close()


if __name__ == "__main__":
    unittest.main()
