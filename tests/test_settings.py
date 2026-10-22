"""The Settings layer (app/core/settings.py): validation, defaults, applying.

Run from the project root:  ./.venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from app.core import machine, settings
from app.core.config import Config
from app.pipeline import tunables as T


def base_config() -> Config:
    return Config(
        project_name="t", config_file=Path("/x/config.yaml"), config_created=False, config_warning="",
        app_dir=Path("/x"), database_path=Path("/x/app.db"), storage_dir=Path("/x/storage"),
        static_dir="", language_file="", host="127.0.0.1", port=8010, secure_cookies=False,
        cookie_name="session", session_ttl_seconds=1, login_token_ttl_seconds=1, api_keys_enabled=True,
        api_key_ttl_seconds=1, secret_key="0" * 64,
        google_redirect_url="http://localhost:8010/api/google/callback",
    )


class CpuLimitTests(unittest.TestCase):
    def test_no_limit_keeps_the_configured_workers(self) -> None:
        cfg = dataclasses.replace(base_config(), jobs_scan_workers=11, jobs_extract_workers=6, jobs_cpu_limit=0)
        self.assertEqual(machine.worker_plan(cfg), (11, 6))

    def test_limit_is_shared_between_videos(self) -> None:
        cfg = dataclasses.replace(base_config(), jobs_scan_workers=11, jobs_extract_workers=6, jobs_cpu_limit=4)
        self.assertEqual(machine.worker_plan(cfg), (4, 4))
        two = dataclasses.replace(cfg, jobs_max_concurrent=2)
        self.assertEqual(machine.worker_plan(two), (2, 2))
        tiny = dataclasses.replace(cfg, jobs_cpu_limit=1, jobs_max_concurrent=3)
        self.assertEqual(machine.worker_plan(tiny), (1, 1))

    def test_setting_and_machine_on_the_page(self) -> None:
        self.assertEqual(settings.merged({}, {"cpu_limit": "3"}), {"cpu_limit": 3})
        with self.assertRaises(settings.SettingsError):
            settings.merged({}, {"cpu_limit": 2.5})
        page = settings.describe(base_config(), {"cpu_limit": 3})
        field = next(f for g in page["groups"] for f in g["fields"] if f["key"] == "cpu_limit")
        self.assertEqual((field["value"], field["max"]), (3, machine.detect().cores))
        self.assertGreaterEqual(page["machine"]["cores"], 1)
        self.assertTrue(page["machine"]["cpu"])


class SettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        settings.apply_tunables({})  # leave the module as shipped

    def test_values_are_checked_against_bounds(self) -> None:
        with self.assertRaises(settings.SettingsError):
            settings.merged({}, {"stride": 0})
        with self.assertRaises(settings.SettingsError):
            settings.merged({}, {"stride": 2.5})
        with self.assertRaises(settings.SettingsError):
            settings.merged({}, {"youtube_max_height": 999})
        with self.assertRaises(settings.SettingsError):
            settings.merged({}, {"no_such_setting": 1})
        self.assertEqual(settings.merged({}, {"stride": "7"}), {"stride": 7})

    def test_null_resets_and_tunables_return_to_their_defaults(self) -> None:
        shipped = T.STRIDE
        overrides = settings.merged({}, {"stride": shipped + 3})
        settings.apply_tunables(overrides)
        self.assertEqual(T.STRIDE, shipped + 3)
        overrides = settings.merged(overrides, {"stride": None})
        settings.apply_tunables(overrides)
        self.assertEqual(overrides, {})
        self.assertEqual(T.STRIDE, shipped)

    def test_config_values_scale_and_resolve(self) -> None:
        overrides = settings.merged({}, {"upload_max_gb": 2, "scan_workers": 0, "google_redirect_url": ""})
        effective = settings.apply_config(base_config(), overrides)
        self.assertEqual(effective.upload_max_bytes, 2 * 1024 ** 3)
        self.assertGreaterEqual(effective.jobs_scan_workers, 1)
        self.assertEqual(effective.google_redirect_url, "http://localhost:8010/api/google/callback")

    def test_secret_is_never_sent_back_and_blank_keeps_it(self) -> None:
        overrides = settings.merged({}, {"google_client_id": "id.apps.googleusercontent.com",
                                         "google_client_secret": "s3cret"})
        overrides = settings.merged(overrides, {"google_client_secret": ""})
        self.assertEqual(overrides["google_client_secret"], "s3cret")
        described = settings.describe(base_config(), overrides)
        google = next(group for group in described["groups"] if group["id"] == "google")
        secret = next(field for field in google["fields"] if field["key"] == "google_client_secret")
        self.assertEqual(secret["value"], "")
        self.assertTrue(secret["set"])
        self.assertNotIn("s3cret", repr(described))

    def test_client_id_without_secret_is_refused(self) -> None:
        overrides = settings.merged({}, {"google_client_id": "id.apps.googleusercontent.com"})
        with self.assertRaises(settings.SettingsError):
            settings.check(settings.apply_config(base_config(), overrides))

    def test_every_field_has_a_label(self) -> None:
        import yaml

        language = yaml.safe_load(Path(__file__).resolve().parent.parent.joinpath("language.yaml").read_text())
        fields = language["settings"]["fields"]
        for field in settings.FIELDS:
            self.assertIn(field.key, fields, f"language.yaml settings.fields.{field.key} is missing")
            self.assertTrue(fields[field.key].get("label"), field.key)
        for group in settings.GROUPS:
            self.assertTrue(language["settings"]["groups"].get(group), group)

    def test_base_config_is_untouched(self) -> None:
        base = base_config()
        settings.apply_config(base, settings.merged({}, {"max_concurrent": 3}))
        self.assertEqual(base.jobs_max_concurrent, 1)
        self.assertTrue(dataclasses.is_dataclass(base))


if __name__ == "__main__":
    unittest.main()
