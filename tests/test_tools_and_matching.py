from __future__ import annotations

import unittest
from typing import Any

from mcstatus_mcp.client import MCStatusApiClient
from mcstatus_mcp.tools import CheckNodeStatusTool


class _FakeToolApiClient:
    def check_node_status(self, node_name: str, timeout_ms: int | None = None) -> dict[str, object]:
        return {
            "ok": True,
            "input_node_name": node_name,
            "timeout_ms": timeout_ms,
        }


class _FakeKumaApiClient(MCStatusApiClient):
    def __init__(self) -> None:
        super().__init__(
            base_url="https://example.invalid/api",
            kuma_api_base_url="https://example.invalid/api",
        )
        self._nodes_payload = {
            "publicGroupList": [
                {
                    "monitorList": [
                        {"id": 1, "name": "x21.oinserver.xyz"},
                        {"id": 2, "name": "fra9.joinserver.xyz"},
                        {"id": 3, "name": "MySQL-FRA9"},
                        {"id": 4, "name": "MySQL-RU-R1"},
                        {"id": 5, "name": "HMFRA1-7950"},
                        {"id": 6, "name": "HMFRA1-R9"},
                    ]
                }
            ]
        }
        self._heartbeat_payload = {
            "heartbeatList": {
                "1": [{"status": 0, "time": "2026-03-15 13:21:23", "msg": "", "ping": None}],
                "2": [{"status": 1, "time": "2026-03-15 13:21:24", "msg": "", "ping": 14}],
                "3": [{"status": 2, "time": "2026-03-15 13:21:25", "msg": "warming up", "ping": 31}],
                "4": [{"status": 0, "time": "2026-03-15 13:21:26", "msg": "down", "ping": None}],
                "5": [{"status": 1, "time": "2026-03-15 13:21:27", "msg": "", "ping": 8}],
                "6": [{"status": 0, "time": "2026-03-15 13:21:28", "msg": "legacy", "ping": None}],
            }
        }

    def _request_json_with_base(
        self,
        *,
        base_url: str,
        source_name: str,
        path: str,
        params: dict[str, Any],
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if path == "status-page/nodes":
            return self._nodes_payload
        if path == "status-page/heartbeat/nodes":
            return self._heartbeat_payload
        raise AssertionError(f"Unexpected path: {path}")


class ToolResultCompatibilityTests(unittest.TestCase):
    def test_check_node_status_tool_returns_plain_dict(self) -> None:
        tool = CheckNodeStatusTool(_FakeToolApiClient())  # type: ignore[arg-type]

        result = tool.invoke(node_name="Node-x21", timeout_ms=4000)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["input_node_name"], "Node-x21")
        self.assertEqual(result["timeout_ms"], 4000)


class KumaNodeMatchingTests(unittest.TestCase):
    def test_matches_gpt_style_x21_queries(self) -> None:
        cases = [
            ("x21", "x21.oinserver.xyz", (2, "short_hostname")),
            ("X21", "x21.oinserver.xyz", (2, "short_hostname")),
            ("x 21", "x21.oinserver.xyz", (3, "short_hostname_normalized")),
            ("x-21", "x21.oinserver.xyz", (3, "short_hostname_normalized")),
            ("x_21", "x21.oinserver.xyz", (3, "short_hostname_normalized")),
            ("Node-x21", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("node x 21", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("Nodex21", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("x21 node", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("Нода-x21", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("сервер x21", "x21.oinserver.xyz", (5, "core_fingerprint")),
            ("host x21", "x21.oinserver.xyz", (5, "core_fingerprint")),
        ]

        for query, monitor_name, expected in cases:
            with self.subTest(query=query, monitor_name=monitor_name):
                match = MCStatusApiClient._match_kuma_node_name(query, monitor_name)
                self.assertEqual(match, expected)

    def test_matches_gpt_style_mysql_queries(self) -> None:
        cases = [
            ("mysql", "MySQL-FRA9", (6, "core_terms_subset")),
            ("mysql fra9", "MySQL-FRA9", (3, "short_hostname_normalized")),
            ("mysql fra 9", "MySQL-FRA9", (3, "short_hostname_normalized")),
            ("fra9 mysql", "MySQL-FRA9", (6, "core_terms_subset")),
            ("ru r1", "MySQL-RU-R1", (6, "core_terms_subset")),
            ("mysql ru r1", "MySQL-RU-R1", (3, "short_hostname_normalized")),
            ("mysql r1 ru", "MySQL-RU-R1", (6, "core_terms_subset")),
        ]

        for query, monitor_name, expected in cases:
            with self.subTest(query=query, monitor_name=monitor_name):
                match = MCStatusApiClient._match_kuma_node_name(query, monitor_name)
                self.assertEqual(match, expected)

    def test_rejects_non_matching_noisy_queries(self) -> None:
        cases = [
            ("unknown x21", "x21.oinserver.xyz"),
            ("panel x21", "x21.oinserver.xyz"),
            ("x22", "x21.oinserver.xyz"),
            ("mysql ru r2", "MySQL-RU-R1"),
        ]

        for query, monitor_name in cases:
            with self.subTest(query=query, monitor_name=monitor_name):
                match = MCStatusApiClient._match_kuma_node_name(query, monitor_name)
                self.assertIsNone(match)


class CheckNodeStatusBehaviorTests(unittest.TestCase):
    def test_single_match_gpt_style_queries(self) -> None:
        client = _FakeKumaApiClient()
        cases = [
            ("x21", "x21.oinserver.xyz", "short_hostname", "x21"),
            ("x 21", "x21.oinserver.xyz", "short_hostname_normalized", "x21"),
            ("Node-x21", "x21.oinserver.xyz", "core_fingerprint", "x21"),
            ("Нода-x21", "x21.oinserver.xyz", "core_fingerprint", "x21"),
            ("mysql fra 9", "MySQL-FRA9", "short_hostname_normalized", "mysqlfra9"),
            ("ru r1", "MySQL-RU-R1", "core_terms_subset", "rur1"),
            ("fra9", "fra9.joinserver.xyz", "short_hostname", "fra9"),
        ]

        for query, expected_name, expected_mode, expected_fingerprint in cases:
            with self.subTest(query=query):
                result = client.check_node_status(query, timeout_ms=4000)
                self.assertTrue(result["ok"])
                self.assertEqual(result["node_name"], expected_name)
                self.assertEqual(result["matched_by"], expected_mode)
                self.assertEqual(result["interpreted_query"]["core_fingerprint"], expected_fingerprint)

    def test_multiple_best_matches_return_statuses_for_all(self) -> None:
        client = _FakeKumaApiClient()
        cases = [
            ("fra", ["fra9.joinserver.xyz", "MySQL-FRA9"]),
            ("mysql", ["MySQL-FRA9", "MySQL-RU-R1"]),
            ("hmfra1", ["HMFRA1-7950", "HMFRA1-R9"]),
        ]

        for query, expected_names in cases:
            with self.subTest(query=query):
                result = client.check_node_status(query, timeout_ms=4000)
                self.assertTrue(result["ok"])
                self.assertTrue(result["ambiguous"])
                self.assertEqual(result["match_count"], len(expected_names))
                self.assertEqual([match["node_name"] for match in result["matches"]], expected_names)
                self.assertEqual(result["matched_by_modes"], ["core_terms_subset"])

    def test_not_found_queries_return_interpreted_error_payload(self) -> None:
        client = _FakeKumaApiClient()
        cases = [
            ("unknown x21", "unknownx21"),
            ("panel fra", "panelfra"),
        ]

        for query, expected_fingerprint in cases:
            with self.subTest(query=query):
                result = client.check_node_status(query, timeout_ms=4000)
                self.assertFalse(result["ok"])
                self.assertEqual(result["interpreted_query"]["core_fingerprint"], expected_fingerprint)
                self.assertIn("not found", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
