# encoding:utf-8
"""
Unit tests for the web_search tool, focused on the AnySearch and Serply
backends.

Covers key resolution (config file + environment fallback), the canonical
provider fallback order, result normalization into the unified output shape,
the AnySearch-specific count clamp (the shared tool schema allows 1-50 while
the API accepts 1-20), HTTP and business-level error mapping, and the
anonymous mode contract (no Authorization header without a key).

No real network is used: ``requests.post`` / ``requests.get`` are stubbed
throughout.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.tools.web_search import web_search as web_search_module


def _fake_response(status_code=200, payload=None):
    """Build a minimal stand-in for a requests Response."""
    resp = MagicMock()
    resp.status_code = status_code
    body = payload if payload is not None else {}
    resp.json = lambda: body
    resp.text = json.dumps(body)
    return resp


def _anysearch_payload(results, total=None, code=0, message="success"):
    """Build an AnySearch /v1/search response body in the documented shape."""
    return {
        "code": code,
        "message": message,
        "request_id": "req-test",
        "data": {
            "results": results,
            "metadata": {"total_results": total if total is not None else len(results)},
        },
    }


class TestAnySearchKeyResolution(unittest.TestCase):
    """The anysearch key lives under tools.web_search, falling back to env."""

    def setUp(self):
        self._prev_env = os.environ.get("ANYSEARCH_API_KEY")
        os.environ.pop("ANYSEARCH_API_KEY", None)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("ANYSEARCH_API_KEY", None)
        else:
            os.environ["ANYSEARCH_API_KEY"] = self._prev_env

    def test_anysearch_key_from_tools_config(self):
        """tools.web_search.anysearch_api_key is resolved, and wins over env."""
        cfg = {"tools": {"web_search": {"anysearch_api_key": "test-key-123"}}}
        with patch.object(web_search_module, "conf", lambda: cfg):
            self.assertEqual(web_search_module._get_api_key("anysearch"), "test-key-123")
            with patch.dict(os.environ, {"ANYSEARCH_API_KEY": "env-key-456"}):
                self.assertEqual(web_search_module._get_api_key("anysearch"), "test-key-123")

    def test_anysearch_key_env_fallback(self):
        """Without a config value, ANYSEARCH_API_KEY is used; both empty -> ''."""
        with patch.object(web_search_module, "conf", lambda: {}):
            with patch.dict(os.environ, {"ANYSEARCH_API_KEY": "env-key-456"}):
                self.assertEqual(web_search_module._get_api_key("anysearch"), "env-key-456")
            self.assertEqual(web_search_module._get_api_key("anysearch"), "")


class TestProviderOrder(unittest.TestCase):
    """New providers are appended after the four originals, leaving existing
    routing untouched: anysearch first, then serply."""

    def test_provider_order_appends_new_providers_last(self):
        self.assertEqual(
            web_search_module.PROVIDER_ORDER,
            ("bocha", "qianfan", "zhipu", "linkai", "anysearch", "serply"),
        )


class TestAnySearchBackend(unittest.TestCase):
    """Behaviour of WebSearch._search_anysearch with a stubbed HTTP layer."""

    def setUp(self):
        self.tool = web_search_module.WebSearch()

    def test_search_anysearch_maps_results(self):
        """Results under data.results map to the unified output shape.

        `total` comes from metadata.total_results; a missing snippet falls
        back to content, truncated to 200 chars. With a key configured, the
        Authorization header is sent.
        """
        long_content = "x" * 300
        payload = _anysearch_payload(
            [
                {"title": "T1", "url": "https://a.example/1", "snippet": "s1", "content": "c1"},
                {"title": "T2", "url": "https://a.example/2", "content": long_content},
            ],
            total=123,
        )
        with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                patch.object(web_search_module.requests, "post",
                             return_value=_fake_response(200, payload)) as mock_post:
            result = self.tool._search_anysearch("cowagent", 10)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["backend"], "anysearch")
        self.assertEqual(result.result["total"], 123)
        self.assertEqual(result.result["count"], 2)
        first, second = result.result["results"]
        self.assertEqual(first, {"title": "T1", "url": "https://a.example/1", "snippet": "s1"})
        self.assertEqual(second["snippet"], long_content[:200])
        headers = mock_post.call_args[1]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer test-key-123")

    def test_search_anysearch_clamps_count(self):
        """count is clamped into AnySearch's documented 1-20 range.

        A falsy count falls back to the default of 10, matching the
        `count or 10` idiom used by the zhipu/qianfan backends (execute()
        already normalizes out-of-range values to 10 before dispatch).
        """
        with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                patch.object(web_search_module.requests, "post",
                             return_value=_fake_response(200, _anysearch_payload([]))) as mock_post:
            for count, expected in ((50, 20), (0, 10), (None, 10), (10, 10)):
                with self.subTest(count=count):
                    self.tool._search_anysearch("q", count)
                    self.assertEqual(mock_post.call_args[1]["json"]["max_results"], expected)

    def test_search_anysearch_error_status_mapping(self):
        """401/402/429 map to specific messages; other statuses are generic."""
        cases = {
            401: "Invalid AnySearch API key",
            402: "quota exhausted",
            429: "rate limit reached",
            500: "HTTP 500",
        }
        for status_code, fragment in cases.items():
            with self.subTest(status_code=status_code):
                with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                        patch.object(web_search_module.requests, "post",
                                     return_value=_fake_response(status_code)):
                    result = self.tool._search_anysearch("q", 10)
                self.assertEqual(result.status, "error")
                self.assertIn(fragment, result.result)

    def test_search_anysearch_business_error(self):
        """HTTP 200 with a non-zero business code is surfaced as an error."""
        payload = {"code": 40001, "message": "invalid api key", "request_id": "req-test"}
        with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                patch.object(web_search_module.requests, "post",
                             return_value=_fake_response(200, payload)):
            result = self.tool._search_anysearch("q", 10)
        self.assertEqual(result.status, "error")
        self.assertIn("code=40001", result.result)
        self.assertIn("invalid api key", result.result)

    def test_search_anysearch_omits_auth_header_without_key(self):
        """Anonymous mode: without a key, no Authorization header is sent."""
        with patch.object(web_search_module, "_get_api_key", return_value=""), \
                patch.object(web_search_module.requests, "post",
                             return_value=_fake_response(200, _anysearch_payload([]))) as mock_post:
            result = self.tool._search_anysearch("q", 10)
        self.assertEqual(result.status, "success")
        headers = mock_post.call_args[1]["headers"]
        self.assertNotIn("Authorization", headers)


def _serply_payload(results):
    """Build a Serply /v1/search response body in the documented shape."""
    return {"results": results, "ads": [], "related_questions": []}


class TestSerplyKeyResolution(unittest.TestCase):
    """The serply key lives under tools.web_search, falling back to env."""

    def setUp(self):
        self._prev_env = os.environ.get("SERPLY_API_KEY")
        os.environ.pop("SERPLY_API_KEY", None)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("SERPLY_API_KEY", None)
        else:
            os.environ["SERPLY_API_KEY"] = self._prev_env

    def test_serply_key_from_tools_config(self):
        """tools.web_search.serply_api_key is resolved, and wins over env."""
        cfg = {"tools": {"web_search": {"serply_api_key": "test-key-123"}}}
        with patch.object(web_search_module, "conf", lambda: cfg):
            self.assertEqual(web_search_module._get_api_key("serply"), "test-key-123")
            with patch.dict(os.environ, {"SERPLY_API_KEY": "env-key-456"}):
                self.assertEqual(web_search_module._get_api_key("serply"), "test-key-123")

    def test_serply_key_env_fallback(self):
        """Without a config value, SERPLY_API_KEY is used; both empty -> ''."""
        with patch.object(web_search_module, "conf", lambda: {}):
            with patch.dict(os.environ, {"SERPLY_API_KEY": "env-key-456"}):
                self.assertEqual(web_search_module._get_api_key("serply"), "env-key-456")
            self.assertEqual(web_search_module._get_api_key("serply"), "")


class TestSerplyBackend(unittest.TestCase):
    """Behaviour of WebSearch._search_serply with a stubbed HTTP layer."""

    def setUp(self):
        self.tool = web_search_module.WebSearch()

    def test_search_serply_maps_results(self):
        """Serply's title/link/description map to title/url/snippet, and the
        key travels in the X-Api-Key header alongside an explicit User-Agent."""
        payload = _serply_payload([
            {"title": "T1", "link": "https://a.example/1", "description": "s1"},
            {"title": "T2", "link": "https://a.example/2"},
        ])
        with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                patch.object(web_search_module.requests, "get",
                             return_value=_fake_response(200, payload)) as mock_get:
            result = self.tool._search_serply("cowagent", 10)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["backend"], "serply")
        self.assertEqual(result.result["total"], 2)
        self.assertEqual(result.result["count"], 2)
        first, second = result.result["results"]
        self.assertEqual(first, {"title": "T1", "url": "https://a.example/1", "snippet": "s1"})
        self.assertEqual(second, {"title": "T2", "url": "https://a.example/2", "snippet": ""})
        headers = mock_get.call_args[1]["headers"]
        self.assertEqual(headers.get("X-Api-Key"), "test-key-123")
        self.assertTrue(headers.get("User-Agent"))

    def test_search_serply_clamps_count(self):
        """count is clamped into 1-50 and sent as the `num` query parameter;
        a falsy count falls back to the default of 10."""
        with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                patch.object(web_search_module.requests, "get",
                             return_value=_fake_response(200, _serply_payload([]))) as mock_get:
            for count, expected in ((50, 50), (99, 50), (0, 10), (None, 10), (10, 10)):
                with self.subTest(count=count):
                    self.tool._search_serply("q", count)
                    url = mock_get.call_args[0][0]
                    self.assertIn(f"num={expected}", url)
                    self.assertIn("q=q", url)

    def test_search_serply_error_status_mapping(self):
        """401/429 map to specific messages; other statuses are generic."""
        cases = {
            401: "Invalid Serply API key",
            429: "rate limit reached",
            500: "HTTP 500",
        }
        for status_code, fragment in cases.items():
            with self.subTest(status_code=status_code):
                with patch.object(web_search_module, "_get_api_key", return_value="test-key-123"), \
                        patch.object(web_search_module.requests, "get",
                                     return_value=_fake_response(status_code)):
                    result = self.tool._search_serply("q", 10)
                self.assertEqual(result.status, "error")
                self.assertIn(fragment, result.result)


if __name__ == "__main__":
    unittest.main()
