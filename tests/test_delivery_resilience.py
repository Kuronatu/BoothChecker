import asyncio
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests
import simdjson


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "booth_checker"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load("booth_checker_main", ROOT / "booth_checker" / "__main__.py")
bot = _load("booth_discord_bot", ROOT / "booth_discord" / "booth_discord.py")

import booth  # noqa: E402  (booth_checker/booth.py, resolved via sys.path above)


def _zip_bytes(names, utf8_flag=True, encrypted=False):
    """Builds a stored zip. utf8_flag=False writes raw UTF-8 names without the
    EFS bit (what macOS Finder produces); encrypted=True sets the encryption
    bit so extraction fails like a password-protected archive."""
    placeholders = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, name in enumerate(names):
            raw = name.encode("utf-8")
            if utf8_flag:
                zf.writestr(name, b"data")
            else:
                ph = (chr(ord("A") + i) * len(raw))
                placeholders[ph.encode()] = raw
                zf.writestr(ph, b"data")
    data = buf.getvalue()
    for ph, raw in placeholders.items():
        data = data.replace(ph, raw)
    if encrypted:
        data = bytearray(data)
        for sig, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            pos = data.find(sig)
            while pos != -1:
                data[pos + flag_offset] |= 0x01
                pos = data.find(sig, pos + 4)
        data = bytes(data)
    return data


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class PipelineHarness(unittest.TestCase):
    """Runs the real init_update_check against mocked BOOTH/booth_discord."""

    ORDER = "111"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        for d in ("version/json", "changelog", "work", "archive"):
            os.makedirs(d)
        shutil.copytree(ROOT / "templates", "templates")

        for attr, value in {
            "DRY_RUN": False, "discord_api_url": "http://discord", "s3": None,
            "s3_uploader": None, "gemini_api_key": None, "summary": None,
        }.items():
            patcher = mock.patch.object(checker, attr, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        checker.changelog_failures.clear()

        self.zip_data = _zip_bytes(["readme.txt"])
        self.statuses = {}
        self.raises = {}
        self.posts = []
        for target, fake in (
            ("crawling", self._fake_crawling),
            ("download_item", self._fake_download),
            ("crawling_product", lambda url: None),
        ):
            patcher = mock.patch.object(checker.booth, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(checker.requests, "post", self._fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_crawling(self, order_num, product_only, cookie, shortlist=None, thumblist=None):
        shortlist.append("900")
        return [["900", "item.zip"]], [["Item", "https://booth.pm/ja/items/1"]]

    def _fake_download(self, download_number, filepath, cookie):
        with open(filepath, "wb") as f:
            f.write(self.zip_data)

    def _fake_post(self, url, json=None, timeout=None):
        route = url.rsplit("/", 1)[-1]
        self.posts.append((route, json))
        if route in self.raises:
            raise self.raises[route]
        return FakeResponse(self.statuses.get(route, 200))

    def write_version(self, data):
        with open(f"version/json/{self.ORDER}.json", "w") as f:
            simdjson.dump(data, fp=f)

    def read_version(self):
        with open(f"version/json/{self.ORDER}.json") as f:
            return simdjson.load(f)

    def run_cycle(self, encoding="shift_jis", fbx_only=False):
        checker.reset_users_this_cycle.clear()
        self.posts.clear()
        item = (self.ORDER, 1, None, encoding, True, True, False, False, False, fbx_only, "cookie", "u1", "42")
        checker.init_update_check(item)
        return [route for route, _ in self.posts if route != "reset_error"]

    def payload(self, route):
        return next(body for r, body in self.posts if r == route)


class ChangelogRetryCapTests(PipelineHarness):
    def test_unparseable_archive_notifies_without_changelog_after_cap(self):
        self.zip_data = _zip_bytes(["model.fbx"], encrypted=True)
        self.write_version({"short-list": ["800"], "name-list": ["old.zip"],
                            "files": {"old.zip": {"hash": "abc"}}, "fbx-files": {}})

        for _ in range(checker.CHANGELOG_MAX_ATTEMPTS - 1):
            self.assertEqual(self.run_cycle(), [])
            self.assertEqual(self.read_version()["short-list"], ["800"])

        self.assertEqual(self.run_cycle(), ["send_message"])
        self.assertEqual(self.payload("send_message")["summary"], checker.CHANGELOG_FAILED_NOTE)
        saved = self.read_version()
        self.assertEqual(saved["short-list"], ["900"])
        # The last parsed tree is kept as the baseline, not a half-marked one.
        self.assertEqual(saved["files"], {"old.zip": {"hash": "abc"}})
        self.assertNotIn(self.ORDER, checker.changelog_failures)

        self.assertEqual(self.run_cycle(), [])  # nothing changed any more

    def test_fbx_only_unparseable_archive_also_falls_back(self):
        self.zip_data = _zip_bytes(["model.fbx"], encrypted=True)
        for _ in range(checker.CHANGELOG_MAX_ATTEMPTS - 1):
            self.assertEqual(self.run_cycle(fbx_only=True), [])
        self.assertEqual(self.run_cycle(fbx_only=True), ["send_message"])
        self.assertEqual(self.read_version()["short-list"], ["900"])

    def test_new_download_set_restarts_the_count(self):
        self.assertEqual(checker.record_changelog_failure(self.ORDER, ["1"]), 1)
        self.assertEqual(checker.record_changelog_failure(self.ORDER, ["1"]), 2)
        self.assertEqual(checker.record_changelog_failure(self.ORDER, ["2"]), 1)


class ZipNameEncodingTests(PipelineHarness):
    def test_utf8_names_without_flag_parse_under_shift_jis(self):
        self.zip_data = _zip_bytes(["モデル_一覧.fbx"], utf8_flag=False)
        self.assertEqual(self.run_cycle(encoding="shift_jis"), ["send_message", "send_changelog"])
        files = self.read_version()["files"]["item.zip"]["files"]
        self.assertIn("モデル_一覧.fbx", files)

    def _raw_name_zip(self, raw):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Q" * len(raw), b"data")
        path = os.path.join(self.tmp, "raw.zip")
        with open(path, "wb") as f:
            f.write(buf.getvalue().replace(b"Q" * len(raw), raw))
        return path

    def test_open_zip_keeps_requested_encoding_when_it_works(self):
        path = self._raw_name_zip("テクスチャ.png".encode("cp932"))
        with checker.open_zip(path, "shift_jis") as zf:
            self.assertEqual(zf.namelist(), ["テクスチャ.png"])

    def test_open_zip_tries_requested_encoding_before_utf8(self):
        # UTF-8 bytes that also decode as shift_jis must keep the shift_jis
        # reading, or names already stored in version files would change.
        path = self._raw_name_zip("アイ.fbx".encode("utf-8"))
        with checker.open_zip(path, "shift_jis") as zf:
            self.assertEqual(zf.namelist(), ["アイ.fbx".encode("utf-8").decode("shift_jis")])

    def test_open_zip_decodes_cp932_extensions(self):
        path = self._raw_name_zip("①テクスチャ.png".encode("cp932"))
        with checker.open_zip(path, "shift_jis") as zf:
            self.assertEqual(zf.namelist(), ["①テクスチャ.png"])

    def test_open_zip_survives_unknown_encoding_name(self):
        path = os.path.join(self.tmp, "a.zip")
        with open(path, "wb") as f:
            f.write(_zip_bytes(["a.txt"]))
        with checker.open_zip(path, "not-an-encoding") as zf:
            self.assertEqual(zf.namelist(), ["a.txt"])


class DeliveryOutcomeTests(PipelineHarness):
    def test_changelog_upload_failure_does_not_resend_embed(self):
        for failure in ({"statuses": 502}, {"raises": requests.ConnectionError()}):
            with self.subTest(failure=failure):
                self.write_version({"short-list": [], "name-list": [], "files": {}, "fbx-files": {}})
                self.statuses.clear()
                self.raises.clear()
                if "statuses" in failure:
                    self.statuses["send_changelog"] = failure["statuses"]
                else:
                    self.raises["send_changelog"] = failure["raises"]
                self.assertEqual(self.run_cycle(), ["send_message", "send_changelog"])
                self.assertEqual(self.read_version()["short-list"], ["900"])
                self.assertEqual(self.run_cycle(), [])

    def test_permanent_rejection_advances_version(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.write_version({"short-list": [], "name-list": [], "files": {}, "fbx-files": {}})
                self.statuses["send_message"] = status
                self.assertEqual(self.run_cycle(), ["send_message"])
                self.assertEqual(self.read_version()["short-list"], ["900"])

    def test_transient_failure_keeps_version_for_retry(self):
        self.statuses["send_message"] = 502
        self.assertEqual(self.run_cycle(), ["send_message"])
        self.assertEqual(self.read_version()["short-list"], [])
        self.statuses["send_message"] = 200
        self.assertEqual(self.run_cycle(), ["send_message", "send_changelog"])
        self.assertEqual(self.read_version()["short-list"], ["900"])


class CrawlingProductTests(unittest.TestCase):
    def test_http_error_returns_none(self):
        response = requests.Response()
        response.status_code = 404
        with mock.patch.object(booth.requests, "get", return_value=response):
            self.assertIsNone(booth.crawling_product("https://booth.pm/ja/items/1"))

    def test_connection_error_returns_none(self):
        with mock.patch.object(booth.requests, "get", side_effect=requests.ConnectionError()):
            self.assertIsNone(booth.crawling_product("https://booth.pm/ja/items/1"))


class DiscordLimitsTests(unittest.TestCase):
    def _http_error(self, cls, status):
        return cls(types.SimpleNamespace(status=status, reason="x"), "boom")

    def test_permanent_discord_errors_map_to_422(self):
        self.assertEqual(bot._failure_status(self._http_error(bot.discord.NotFound, 404)), 422)
        self.assertEqual(bot._failure_status(self._http_error(bot.discord.Forbidden, 403)), 422)
        self.assertEqual(bot._failure_status(self._http_error(bot.discord.HTTPException, 400)), 422)

    def test_transient_errors_stay_502(self):
        self.assertEqual(bot._failure_status(self._http_error(bot.discord.DiscordServerError, 503)), 502)
        self.assertEqual(bot._failure_status(self._http_error(bot.discord.HTTPException, 429)), 502)
        self.assertEqual(bot._failure_status(RuntimeError("x")), 502)

    def test_clip_field_fits_limit_at_line_boundary(self):
        names = [f"Outfit_for_Avatar{i:02d}_v1.2.3.zip" for i in range(60)]
        clipped = bot._clip_field("\n".join(names))
        self.assertLessEqual(len(clipped), bot.EMBED_FIELD_LIMIT)
        lines = clipped.split("\n")
        self.assertEqual(lines[:-1], names[:len(lines) - 1])
        self.assertEqual(lines[-1], f"… 외 {60 - (len(lines) - 1)}개")

    def test_clip_field_leaves_short_values_alone(self):
        self.assertEqual(bot._clip_field("a\nb"), "a\nb")

    def _sent_embed(self, author_info, file_list="a.zip"):
        sent = {}

        class Channel:
            async def send(self, content=None, embed=None):
                sent["embed"] = embed

        fake_bot = types.SimpleNamespace(get_channel=lambda channel_id: Channel())
        asyncio.run(bot.DiscordBot.send_message(
            fake_bot, "Item", "https://booth.pm/ja/items/1", "https://thumb", "1",
            "", file_list, author_info, True, True, "42"))
        return sent["embed"].to_dict()

    def test_embed_omits_unknown_author(self):
        self.assertNotIn("author", self._sent_embed(None))
        self.assertEqual(self._sent_embed(["https://icon", "Shop"])["author"]["name"], "Shop ")

    def test_embed_fields_fit_discord_limit(self):
        names = "\n".join(f"Outfit_for_Avatar{i:02d}_v1.2.3.zip" for i in range(60))
        fields = self._sent_embed(None, names)["fields"]
        self.assertTrue(all(len(f["value"]) <= bot.EMBED_FIELD_LIMIT for f in fields))

    def test_clip_field_handles_single_oversized_line(self):
        clipped = bot._clip_field("x" * 5000)
        self.assertLessEqual(len(clipped), bot.EMBED_FIELD_LIMIT)
        self.assertEqual(clipped, "… 외 1개")


if __name__ == "__main__":
    unittest.main()
