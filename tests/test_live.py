import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from token_tv.usage import (
    extract_codex_banked_resets,
    normalize_claude,
    normalize_codex,
    normalize_gemini,
    normalize_grok,
)
from token_tv.sources import fetch_account, load_config, grok_identity, gemini_identity, scoped_env
from token_tv.state import UsageStore
from token_tv.display import render_page, pages, overview_rows, primary_window


class LiveTests(unittest.TestCase):
    def test_provider_cli_environment_does_not_inherit_other_authentication(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "DO-NOT-INHERIT", "GROK_OAUTH_TOKEN": "DO-NOT-INHERIT", "CLAUDE_CODE_OAUTH_TOKEN": "DO-NOT-INHERIT", "GEMINI_API_KEY": "DO-NOT-INHERIT"}):
            for provider, variable in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("grok", "GROK_HOME"), ("gemini", "GEMINI_HOME")):
                env = scoped_env(provider, "/explicit/account/home")
                self.assertEqual(env[variable], "/explicit/account/home")
                self.assertFalse(any(key in env for key in ("OPENAI_API_KEY", "GROK_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY")))

    def test_codex_uses_reported_window_duration(self):
        data = {"rateLimits": {"primary": {"usedPercent": 78, "windowDurationMins": 10080, "resetsAt": 2000000000}, "secondary": None}}
        self.assertEqual(normalize_codex(data), [{"label": "WEEK", "used_percent": 78.0, "resets_at": 2000000000, "duration_minutes": 10080}])

    def test_codex_extracts_banked_resets(self):
        data = {
            "rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 2000000000}},
            "rateLimitResetCredits": {
                "availableCount": 2,
                "credits": [
                    {"status": "available", "expiresAt": 1793294555},
                    {"status": "available", "expiresAt": 1793500000},
                ],
            },
        }
        banked = extract_codex_banked_resets(data)
        self.assertIsNotNone(banked)
        self.assertEqual(banked["count"], 2)
        self.assertEqual(banked["earliest_expires_at"], 1793294555)

        self.assertIsNone(extract_codex_banked_resets({"rateLimitResetCredits": {"availableCount": 0}}))
        self.assertIsNone(extract_codex_banked_resets({}))

    def test_missing_grok_quota_is_unknown(self):
        self.assertEqual(normalize_grok({"config": {"currentPeriod": {"end": "2026-10-05T00:00:00Z"}}}), [])
        self.assertEqual(normalize_claude({"five_hour": {"utilization": None}, "seven_day": {"utilization": 24, "resets_at": None}})[0]["label"], "WEEK")

    def test_credentials_and_identity_never_enter_snapshot(self):
        account = {"key": "a", "alias": "CLAUDE A", "provider": "claude", "source_home": "/unused", "email": "expected@example.com"}
        with patch("token_tv.sources.claude_payload", return_value=({"account": {"email": "wrong@example.com"}}, {"five_hour": {"utilization": 8}, "access_token": "DO-NOT-EXPORT"})):
            row = fetch_account(account)
        self.assertEqual(row["status"], "identity_mismatch")
        self.assertEqual(row["windows"], [])
        self.assertNotIn("DO-NOT-EXPORT", json.dumps(row))
        self.assertNotIn("wrong@example.com", json.dumps(row))

    def test_configuration_rejects_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"accounts": [{"key": "a", "alias": "A", "provider": "claude", "password": "DO-NOT-USE"}]}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_grok_owner_identity_must_match_expected_email(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps({"issuer::client": {"email": "owner@example.com", "key": "NEVER-EXPORT", "refresh_token": "NEVER-EXPORT"}}))
            self.assertEqual(grok_identity({"source_home": directory}), "owner@example.com")
            account = {"key": "g", "alias": "G", "provider": "grok", "source_home": directory, "email": "wrong@example.com"}
            with patch("token_tv.sources.grok_payload") as billing:
                row = fetch_account(account)
            billing.assert_not_called()
            self.assertEqual(row["status"], "identity_mismatch")
            self.assertNotIn("NEVER-EXPORT", json.dumps(row))

    def test_gemini_normalizes_agy_quota(self):
        sample = {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [{
                        "name": "Gemini Models",
                        "buckets": [
                            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.88, "reset_time": "2026-10-08T14:28:50Z"},
                            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.92, "reset_time": "2026-10-05T14:18:55Z"}
                        ]
                    }]
                }
            }
        }
        windows = normalize_gemini(sample)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["label"], "5H")
        self.assertEqual(windows[0]["used_percent"], 8.0)
        self.assertEqual(windows[0]["duration_minutes"], 300)
        self.assertEqual(windows[1]["label"], "WEEK")
        self.assertEqual(windows[1]["used_percent"], 12.0)
        self.assertEqual(windows[1]["duration_minutes"], 10080)

        # Fallback text format
        text_data = {"response": "Gemini Models\tWeekly Limit Remaining\t88%\t2026-10-08 16:28 CEST\nGemini Models\tFive Hour Limit Remaining\t92%\t2026-10-05 16:18 CEST"}
        text_windows = normalize_gemini(text_data)
        self.assertEqual(len(text_windows), 2)
        self.assertEqual(text_windows[0]["label"], "5H")
        self.assertEqual(text_windows[0]["used_percent"], 8.0)
        self.assertEqual(text_windows[1]["label"], "WEEK")
        self.assertEqual(text_windows[1]["used_percent"], 12.0)

    def test_gemini_owner_identity_must_match_expected_email(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth_creds.json"
            path.write_text(json.dumps({"account": {"email": "owner@google.com", "token": "NEVER-EXPORT"}}))
            self.assertEqual(gemini_identity({"source_home": directory}), "owner@google.com")
            account = {"key": "g", "alias": "GEMINI A", "provider": "gemini", "source_home": directory, "email": "wrong@google.com"}
            with patch("token_tv.sources.gemini_payload") as payload_mock:
                row = fetch_account(account)
            payload_mock.assert_not_called()
            self.assertEqual(row["status"], "identity_mismatch")
            self.assertNotIn("NEVER-EXPORT", json.dumps(row))

    def test_cache_is_per_account_and_failure_preserves_stale_values(self):
        accounts = [{"key": "a", "alias": "A", "provider": "claude"}, {"key": "b", "alias": "B", "provider": "claude"}]
        replies = {
            "a": {"key": "a", "alias": "A", "provider": "claude", "status": "ok", "windows": [{"label": "WEEK", "used_percent": 31, "resets_at": None, "duration_minutes": 10080}], "fetched_at": 100},
            "b": {"key": "b", "alias": "B", "provider": "claude", "status": "auth_required", "windows": [], "fetched_at": 100},
        }
        calls = []
        def fetch(account):
            calls.append(account["key"])
            return copy.deepcopy(replies[account["key"]])
        store = UsageStore(accounts, fetch=fetch)
        store.refresh()
        one = store.snapshot()
        two = store.snapshot()
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(one, two)
        self.assertEqual(one["accounts"]["a"]["windows"][0]["used_percent"], 31)
        self.assertEqual(one["accounts"]["b"]["windows"], [])
        replies["a"].update(status="error", windows=[], fetched_at=200)
        store.refresh()
        row = store.snapshot()["accounts"]["a"]
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["windows"][0]["used_percent"], 31)
        self.assertEqual(row["last_success_at"], 100)
        self.assertEqual(row["error_code"], "error")

    def test_wrong_identity_does_not_reuse_previous_account_values(self):
        account = {"key": "a", "alias": "A", "provider": "claude"}
        replies = [dict(account, status="ok", windows=[{"label": "WEEK", "used_percent": 31}], fetched_at=time.time(), identity_verified=True),
                   dict(account, status="identity_mismatch", windows=[], fetched_at=time.time(), identity_verified=False)]
        store = UsageStore([account], fetch=lambda _: replies.pop(0))
        store.refresh()
        store.refresh()
        row = store.snapshot()["accounts"]["a"]
        self.assertEqual(row["status"], "identity_mismatch")
        self.assertEqual(row["windows"], [])

    def test_laptop_fallback_is_replaced_by_verified_mini_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.json"
            row = {"key": "a", "alias": "A", "provider": "claude", "status": "ok", "fetched_at": time.time(), "identity_verified": True,
                   "windows": [{"label": "WEEK", "used_percent": 31, "resets_at": None, "duration_minutes": 10080}]}
            path.write_text(json.dumps({"accounts": {"a": row}}))
            account = {"key": "a", "alias": "A", "provider": "claude", "email": "owner@example.com", "source_home": str(Path(directory)/"absent"), "fallback_snapshot_file": str(path)}
            actual = fetch_account(account)
            self.assertEqual(actual["source"], "laptop")
            self.assertEqual(actual["status"], "ok")
            self.assertEqual(actual["mini_auth_status"], "auth_required")
            with patch("token_tv.sources.claude_payload", return_value=({"account": {"email": "owner@example.com"}}, {"seven_day": {"utilization": 24}})):
                actual = fetch_account(account)
            self.assertEqual(actual["source"], "mini")
            self.assertEqual(actual["windows"][0]["used_percent"], 24)
            self.assertNotIn("mini_auth_status", actual)

    def test_renderer_has_real_pixel_dimensions_and_missing_state(self):
        snapshot = {"schema": 1, "accounts": {"a": {"alias": "CLAUDE A", "provider": "claude", "status": "auth_required", "windows": [], "fetched_at": 100}}}
        self.assertEqual(len(pages(snapshot)), 1)
        body = render_page(snapshot, 0)
        image = Image.open(io.BytesIO(body))
        self.assertEqual(image.size, (240, 240))
        self.assertEqual(image.format, "JPEG")
        self.assertGreater(len(image.getcolors(240 * 240)), 20)
        self.assertLess(len(body), 60000)

    def test_overview_keeps_one_account_per_provider_without_losing_details(self):
        def account(provider, alias, status="ok"):
            return {"provider": provider, "alias": alias, "status": status,
                    "windows": [] if status != "ok" else [{"label": "WEEK", "used_percent": 94, "resets_at": None}]}
        snapshot = {"accounts": {
            "ca": account("claude", "CLAUDE A"), "cb": account("claude", "CLAUDE B"),
            "cc": account("claude", "CLAUDE C"), "xa": account("codex", "CODEX A"),
            "ga": account("grok", "GROK A", "auth_required"), "gb": account("grok", "GROK B")}}
        rows = overview_rows(snapshot)
        self.assertEqual([r["key"] for r in rows], ["ca", "xa", "gb"])
        self.assertEqual(len(snapshot["accounts"]), 6)
        self.assertEqual(render_page(snapshot, 0), render_page(snapshot, 1))

    def test_overview_preserves_quota_period_and_unknown_values(self):
        windows = [{"label": "5H", "used_percent": 2}, {"label": "WEEK", "used_percent": 94}]
        self.assertEqual(primary_window({"windows": windows})["label"], "WEEK")
        self.assertIsNone(primary_window({"windows": []}))
        self.assertEqual(primary_window({"windows": [{"label": "BUDGET", "used_percent": 100}]})["label"], "BUDGET")

    def test_normalize_device_targets_and_preferences(self):
        from token_tv.sources import normalize_device_targets
        from token_tv.live import DisplayPreferences

        c1 = {"accounts": [{"key": "a", "alias": "A", "provider": "codex", "email": "a@x.com"}],
              "device_url": "http://10.0.0.128", "display_style": "hud"}
        t1 = normalize_device_targets(c1)
        self.assertEqual(len(t1), 1)
        self.assertEqual(t1[0]["url"], "http://10.0.0.128")
        self.assertEqual(t1[0]["style"], "hud")

        c2 = {
            "accounts": [{"key": "a", "alias": "A", "provider": "codex", "email": "a@x.com"}],
            "display_style": "hud",
            "device_urls": [
                "http://10.0.0.128",
                {"url": "http://10.0.0.129", "style": "retro", "name": "Office"},
            ],
        }
        t2 = normalize_device_targets(c2)
        self.assertEqual(len(t2), 2)
        self.assertEqual(t2[0]["url"], "http://10.0.0.128")
        self.assertEqual(t2[0]["style"], "hud")
        self.assertIsNone(t2[0]["configured_style"])
        self.assertEqual(t2[1]["url"], "http://10.0.0.129")
        self.assertEqual(t2[1]["style"], "retro")
        self.assertEqual(t2[1]["name"], "Office")

        with tempfile.NamedTemporaryFile("w") as tf:
            tf.write(json.dumps(c2))
            tf.flush()
            prefs = DisplayPreferences(tf.name, c2)
            snap = prefs.snapshot()
            self.assertEqual(len(snap["targets"]), 2)
            self.assertEqual(snap["status"], "queued")

            prefs.update_target("http://10.0.0.128", "ok", applied_style="hud")
            prefs.update_target("http://10.0.0.129", "offline")
            snap2 = prefs.snapshot()
            self.assertEqual(snap2["status"], "ok")
            t_map = {t["url"]: t for t in snap2["targets"]}
            self.assertEqual(t_map["http://10.0.0.128"]["status"], "ok")
            self.assertEqual(t_map["http://10.0.0.129"]["status"], "offline")

    def test_multi_device_liveness_skips_offline_and_updates_online(self):
        from token_tv.device import PhotoDisplay
        from token_tv.live import DisplayPreferences
        from token_tv.sources import normalize_device_targets

        config = {
            "accounts": [{"key": "a", "alias": "A", "provider": "codex", "email": "a@x.com"}],
            "display_style": "hud",
            "device_urls": [
                "http://10.0.0.128",
                {"url": "http://10.0.0.129", "style": "retro", "name": "Desk 2"},
            ],
        }
        with tempfile.NamedTemporaryFile("w") as tf:
            tf.write(json.dumps(config))
            tf.flush()
            prefs = DisplayPreferences(tf.name, config)
            targets = normalize_device_targets(config)
            dev1 = PhotoDisplay("http://10.0.0.128")
            dev2 = PhotoDisplay("http://10.0.0.129")
            with patch.object(dev1, "is_alive", return_value=True), \
                 patch.object(dev2, "is_alive", return_value=False), \
                 patch.object(dev1, "upload", return_value={"name": "tokentv.jpg"}), \
                 patch.object(dev2, "upload") as dev2_upload:
                devices = {"http://10.0.0.128": dev1, "http://10.0.0.129": dev2}
                for t in targets:
                    dev = devices[t["url"]]
                    if not dev.is_alive(timeout=2.0):
                        prefs.update_target(t["url"], "offline")
                    else:
                        dev.upload("tokentv.jpg", b"image")
                        prefs.update_target(t["url"], "ok", applied_style=t["style"])

                dev2_upload.assert_not_called()
                snap = prefs.snapshot()
                self.assertEqual(snap["targets"][0]["status"], "ok")
                self.assertEqual(snap["targets"][1]["status"], "offline")


if __name__ == "__main__":
    unittest.main()

