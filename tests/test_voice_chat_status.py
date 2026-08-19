from __future__ import annotations

import socket
import unittest
import uuid
from unittest.mock import patch

from mcstatus_mcp.client import MCStatusApiClient, SIMPLE_VOICE_CHAT_PING_CHECK_ID
from mcstatus_mcp.tools import CheckVoiceChatStatusTool, build_default_tools


class _FakeUdpSocket:
    def __init__(self, mode: str = "reply") -> None:
        self.mode = mode
        self.sent = b""
        self.connected_to: tuple[object, ...] | None = None
        self.closed = False
        self.recv_count = 0

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[object, ...]) -> None:
        self.connected_to = address

    def send(self, data: bytes) -> int:
        self.sent = data
        return len(data)

    def recv(self, size: int) -> bytes:
        self.recv_count += 1
        if self.mode == "timeout":
            raise socket.timeout()
        if self.mode == "invalid_then_reply" and self.recv_count == 1:
            return b"not-a-matching-pong"
        return self.sent[-24:]

    def close(self) -> None:
        self.closed = True


class _FakeVoiceChatApiClient:
    def check_voice_chat_status(self, **kwargs: object) -> dict[str, object]:
        return {"ok": True, "software": "simple_voice_chat", **kwargs}


class VoiceChatToolTests(unittest.TestCase):
    def test_tool_returns_plain_structured_dict(self) -> None:
        tool = CheckVoiceChatStatusTool(_FakeVoiceChatApiClient())  # type: ignore[arg-type]

        result = tool.invoke(
            host="voice.example.test",
            port=24454,
            timeout_ms=750,
            attempts=2,
        )

        self.assertIsInstance(result, dict)
        self.assertEqual(result["host"], "voice.example.test")
        self.assertEqual(result["software"], "simple_voice_chat")
        self.assertEqual(result["attempts"], 2)

    def test_tool_is_registered_by_default(self) -> None:
        client = MCStatusApiClient(base_url="https://example.invalid/api")
        names = [tool.name for tool in build_default_tools(client)]

        self.assertIn("check_voice_chat_status", names)


class SimpleVoiceChatProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MCStatusApiClient(base_url="https://example.invalid/api")
        self.addresses = [
            (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("203.0.113.10", 24454))
        ]

    def test_ping_packet_matches_official_external_ping_v1_format(self) -> None:
        request_id = uuid.UUID("e5298113-60e8-4918-b6db-57482990c3b8")
        timestamp_ms = 1_723_456_789_012

        packet = self.client._encode_simple_voice_chat_ping(request_id, timestamp_ms)

        self.assertEqual(packet[0], 0xFF)
        self.assertEqual(packet[1:17], SIMPLE_VOICE_CHAT_PING_CHECK_ID.bytes)
        self.assertEqual(packet[17], 24)
        self.assertEqual(packet[18:34], request_id.bytes)
        self.assertTrue(
            self.client._is_matching_simple_voice_chat_pong(
                packet[-24:],
                request_id=request_id,
                timestamp_ms=timestamp_ms,
            )
        )

    def test_valid_pongs_prove_udp_endpoint_online(self) -> None:
        sockets = [_FakeUdpSocket("reply"), _FakeUdpSocket("invalid_then_reply")]

        with (
            patch("mcstatus_mcp.client.socket.getaddrinfo", return_value=self.addresses),
            patch("mcstatus_mcp.client.socket.socket", side_effect=sockets),
        ):
            result = self.client.check_voice_chat_status(
                host="voice.example.test",
                port=24454,
                timeout_ms=500,
                attempts=2,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "online")
        self.assertIs(result["reachable"], True)
        self.assertEqual(result["responses_received"], 2)
        self.assertEqual(result["invalid_responses_received"], 1)
        self.assertEqual(result["packet_loss_percent"], 0.0)
        self.assertEqual(result["resolved_address"], "203.0.113.10")
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_no_pong_is_unconfirmed_instead_of_false_offline_claim(self) -> None:
        sockets = [_FakeUdpSocket("timeout"), _FakeUdpSocket("timeout")]

        with (
            patch("mcstatus_mcp.client.socket.getaddrinfo", return_value=self.addresses),
            patch("mcstatus_mcp.client.socket.socket", side_effect=sockets),
        ):
            result = self.client.check_voice_chat_status(
                host="voice.example.test",
                port=24454,
                timeout_ms=100,
                attempts=2,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertIsNone(result["reachable"])
        self.assertEqual(result["error"], "no_valid_ping_response")
        self.assertIn("allow_pings", str(result["explanation"]))

    def test_attempt_count_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "1..10"):
            self.client.check_voice_chat_status(host="voice.example.test", attempts=11)


if __name__ == "__main__":
    unittest.main()
