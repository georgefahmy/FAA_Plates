"""
Unit tests for FAA raw data downloading and extraction utilities.
"""

import io
import unittest
import zipfile
from pathlib import Path
import tempfile

from enhancer.faa_downloader import (
    DEFAULT_EDITION,
    DEFAULT_VOLUMES,
    DownloadProgress,
    extract_volume_zip,
    fetch_available_editions,
    fetch_available_editions_with_metadata,
    get_volume_urls,
)
from recreate_airmate_chartdata import find_latest_dtpp_xml


class TestFAADownloader(unittest.TestCase):
    def test_get_volume_urls(self):
        urls = get_volume_urls("261001", ["A", "B", "C", "D", "E"])
        self.assertEqual(len(urls), 5)
        self.assertEqual(
            urls["DDTPPA"],
            "https://aeronav.faa.gov/upload_313-d/terminal/DDTPPA_261001.zip",
        )
        self.assertEqual(
            urls["DDTPPE"],
            "https://aeronav.faa.gov/upload_313-d/terminal/DDTPPE_261001.zip",
        )

    def test_extract_volume_zip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_file = tmp_path / "mock_volume.zip"
            extract_dir = tmp_path / "extracted"

            # Create mock zip with two files
            with zipfile.ZipFile(zip_file, "w") as zf:
                zf.writestr("d-TPP_Metafile.xml", "<metafile cycle='2610'/>")
                zf.writestr("00001IL16.PDF", b"%PDF-1.4 test")

            callbacks = []
            def _cb(p: DownloadProgress):
                callbacks.append(p)

            count = extract_volume_zip(
                zip_path=zip_file,
                target_extract_dir=extract_dir,
                volume_name="DDTPPE",
                volume_idx=5,
                total_vols=5,
                progress_callback=_cb,
            )

            self.assertEqual(count, 2)
            self.assertTrue((extract_dir / "d-TPP_Metafile.xml").is_file())
            self.assertTrue((extract_dir / "00001IL16.PDF").is_file())
            self.assertGreater(len(callbacks), 0)
            self.assertEqual(callbacks[-1].stage, "extracting")

    def test_find_latest_dtpp_xml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dir1 = tmp_path / "DDTPPE_261001"
            dir2 = tmp_path / "DDTPPE_261029"
            dir1.mkdir()
            dir2.mkdir()

            xml1 = dir1 / "d-TPP_Metafile.xml"
            xml2 = dir2 / "d-TPP_Metafile.xml"
            xml1.write_text("<metafile cycle='2610'/>")
            xml2.write_text("<metafile cycle='2611'/>")

            latest = find_latest_dtpp_xml(tmp_path)
            self.assertIsNotNone(latest)
            self.assertEqual(latest, xml2)

    def test_fetch_available_editions_with_metadata(self):
        editions = fetch_available_editions_with_metadata()
        self.assertIsInstance(editions, list)
        self.assertGreater(len(editions), 0)
        first = editions[0]
        self.assertIn("edition", first)
        self.assertIn("date_range", first)
        self.assertIn("label", first)
        self.assertEqual(len(first["edition"]), 6)


if __name__ == "__main__":
    unittest.main()
