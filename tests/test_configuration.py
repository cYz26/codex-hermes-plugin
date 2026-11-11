import unittest

from hermes_codex_learning.configuration import build_native_config


class ConfigurationTests(unittest.TestCase):
    def test_named_responses_provider_does_not_use_oauth_defaults(self):
        config = build_native_config(version=12, provider="custom:ark", model="ark:gpt-6-1-sol",
                                     api_mode="codex_responses", base_url="http://127.0.0.1:17413/client-retry",
                                     reasoning_effort="max")
        self.assertEqual(config["model"], {"provider": "custom:ark", "default": "ark:gpt-6-1-sol",
                                         "api_mode": "codex_responses"})
        self.assertEqual(config["providers"]["ark"], {"base_url": "http://127.0.0.1:17413/client-retry",
                                                     "api_mode": "codex_responses"})
        self.assertTrue(config["gateway"]["standalone"])
        self.assertTrue(config["auxiliary"]["background_review"]["enabled"])
        self.assertFalse(config["memory"]["write_approval"])
        self.assertFalse(config["skills"]["write_approval"])
        self.assertNotIn("api_key", config["providers"]["ark"])

    def test_legacy_oauth_defaults_remain_compatible(self):
        config = build_native_config(version=12)
        self.assertEqual(config["model"]["provider"], "openai-codex")
        self.assertEqual(config["model"]["default"], "gpt-6.1-sol")
        self.assertNotIn("providers", config)

    def test_custom_route_must_have_an_explicit_endpoint(self):
        with self.assertRaises(ValueError):
            build_native_config(version=12, provider="custom:ark")

    def test_session_affinity_uses_native_dynamic_header_setting(self):
        config = build_native_config(version=12, provider="custom:ark", base_url="http://127.0.0.1:17413/client-retry",
                                     session_affinity_header="x-session-id")
        self.assertEqual(config["providers"]["ark"]["session_affinity_header"], "x-session-id")
        self.assertNotIn("extra_headers", config["providers"]["ark"])

    def test_credentials_and_invalid_endpoint_are_not_written_to_config(self):
        for url in ["file:///tmp/model", "http://user:password@localhost/v1", "http://localhost/v1?token=value",
                    "http://localhost/v1#fragment", "http://localhost/\nvalue"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                build_native_config(version=12, provider="custom:ark", base_url=url)


if __name__ == "__main__":
    unittest.main()
