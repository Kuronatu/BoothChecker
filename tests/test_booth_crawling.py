import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "booth_checker"))

import booth  # noqa: E402


class GiftCrawlingTests(unittest.TestCase):
    def test_collects_every_download_item(self):
        html = b"""
        <div class="rounded-16 bg-white p-40 mobile:px-16 mobile:pt-24 mobile:pb-40 mobile:rounded-none">
          <div class="mt-24 text-left">
            <a href="https://booth.pm/items/123">Milltina</a>
          </div>
          <img src="https://example.com/thumb.png">
          <div class="mt-16">
            <div class="mt-16 desktop:flex desktop:justify-between desktop:items-center">
              <div class="min-w-0 break-words whitespace-pre-line">
                <div class="text-14">Milltina_FTsetting_v1.13.zip</div>
              </div>
              <div class="mt-8 desktop:mt-0 shrink-0 flex items-center gap-8">
                <div></div>
                <div>
                  <div class="js-download-button"
                       data-href="https://booth.pm/downloadables/8740518"
                       data-test="downloadable"><div><button>Download</button></div></div>
                </div>
                <div class="js-download-button w-full"
                     data-dropdown-items='[{"deeplinkDownloadableUrl":"https://booth.pm/downloadables/8740518/deeplink?client=vroid"}]'
                     data-test="other-downloads-button"><div><button>Other Downloads</button></div></div>
              </div>
            </div>
            <div class="gift-download-row">
              <div class="min-w-0 break-words whitespace-pre-line">
                <div class="text-14">Milltina_FTsetting_v1.2.1.zip</div>
              </div>
              <div class="mt-8 desktop:mt-0 shrink-0 flex items-center gap-8">
                <div></div>
                <div>
                  <div class="js-download-button"
                       data-href="https://booth.pm/downloadables/9691232"
                       data-test="downloadable"><div><button>Download</button></div></div>
                </div>
                <div class="js-download-button w-full"
                     data-dropdown-items='[{"deeplinkDownloadableUrl":"https://booth.pm/downloadables/9691232/deeplink?client=vroid"}]'
                     data-test="other-downloads-button"><div><button>Other Downloads</button></div></div>
              </div>
            </div>
          </div>
        </div>
        """
        for row_class in (
            b"mt-16 desktop:flex desktop:justify-between desktop:items-center",
            b"gift-download-row",
        ):
            with self.subTest(row_class=row_class):
                response = mock.Mock(content=html.replace(b"gift-download-row", row_class))
                shortlist = []
                thumblist = []

                with mock.patch.object(booth.requests, "get", return_value=response) as get:
                    downloads, products = booth.crawling_gift(
                        "gift-token", {"session": "cookie"}, shortlist, thumblist
                    )

                get.assert_called_once_with(
                    url="https://booth.pm/gifts/gift-token",
                    cookies={"session": "cookie"}, timeout=30,
                )
                response.raise_for_status.assert_called_once_with()
                self.assertEqual(
                    downloads,
                    [
                        ["8740518", "Milltina_FTsetting_v1.13.zip"],
                        ["9691232", "Milltina_FTsetting_v1.2.1.zip"],
                    ],
                )
                self.assertEqual(shortlist, ["8740518", "9691232"])
                self.assertEqual(products, [["Milltina", "https://booth.pm/items/123"]])
                self.assertEqual(thumblist, ["https://example.com/thumb.png"])


class OrderCrawlingTests(unittest.TestCase):
    def test_collects_legacy_and_current_downloads_with_product_filter(self):
        html = b"""
        <div class="sheet sheet--p400 mobile:pt-[13px] mobile:px-16 mobile:pb-8">
          <a href="https://example.com/shop">Shop</a>
          <a href="https://booth.pm/items/123">Item</a>
          <div class="legacy-list-item__center">
            <div class="min-w-0 u-text-wrap"><b>legacy.zip</b></div>
            <a class="nav-reverse" href="https://booth.pm/downloadables/100">Download</a>
          </div>
          <div class="mt-16 desktop:flex desktop:justify-between desktop:items-center">
            <div class="min-w-0 break-words whitespace-pre-line">current.zip</div>
            <div><div class="js-download-button" data-test="downloadable"
                      data-href="https://booth.pm/downloadables/200"></div></div>
            <div class="js-download-button" data-test="other-downloads-button"></div>
          </div>
          <div class="legacy-list-item__center">
            <div class="flex-[1]"><b>alternate.zip</b></div>
            <a class="nav-reverse" href="https://booth.pm/downloadables/300">Download</a>
          </div>
        </div>
        """
        for product_filter, included in ((None, True), (["123"], True), (["456"], False)):
            with self.subTest(product_filter=product_filter):
                shortlist = []
                with mock.patch.object(booth.requests, "get", return_value=mock.Mock(content=html)):
                    downloads, products = booth.crawling("order", product_filter, {}, shortlist)

                expected = [["100", "legacy.zip"], ["200", "current.zip"], ["300", "alternate.zip"]]
                self.assertEqual(downloads, expected if included else [])
                self.assertEqual(shortlist, ["100", "200", "300"] if included else [])
                self.assertEqual(products, [["Item", "https://booth.pm/items/123"]] if included else [])


if __name__ == "__main__":
    unittest.main()
