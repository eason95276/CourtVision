from __future__ import annotations

import unittest

from v2.pregame_system.nba_stats_diagnostics import classify_response, redact_text, safe_url


class NbaStatsDiagnosticTests(unittest.TestCase):
    def test_classifies_http_refusal_even_when_body_is_html(self):
        result = classify_response(
            403, "text/html", b"<!doctype html><html>Access Denied</html>"
        )
        self.assertEqual(result["classification"], "http_403_forbidden")
        self.assertTrue(result["is_html"])

    def test_detects_rate_limit(self):
        result = classify_response(429, "text/plain", b"slow down")
        self.assertEqual(result["classification"], "http_429_rate_limited")

    def test_detects_empty_html_challenge_and_invalid_json(self):
        self.assertEqual(classify_response(200, "application/json", b"")["classification"], "empty_response")
        challenge = classify_response(
            200, "text/html", b"<html><title>Just a moment</title>Cloudflare</html>"
        )
        self.assertEqual(challenge["classification"], "html_challenge")
        invalid_json = classify_response(200, "application/json", b"not-json")
        self.assertEqual(invalid_json["classification"], "invalid_json")

    def test_redacts_credentials_and_url_query_values(self):
        self.assertEqual(redact_text("api_key=abc123 authorization:Bearer-secret"),
                         "api_key=<redacted> authorization:<redacted>")
        url = safe_url("https://stats.nba.com/stats/boxscoretraditionalv3?GameID=0022500001&token=secret")
        self.assertEqual(url, "https://stats.nba.com/stats/boxscoretraditionalv3?GameID=0022500001")


if __name__ == "__main__":
    unittest.main()
