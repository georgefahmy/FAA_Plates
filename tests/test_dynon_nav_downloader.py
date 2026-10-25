"""
Unit tests for Dynon Aviation & Obstacle Database Downloader.
"""

import tempfile
import unittest
from pathlib import Path

from enhancer.dynon_nav_downloader import (
    DEFAULT_DYNON_NAV_DIR,
    fetch_available_dynon_databases,
)


class TestDynonNavDownloader(unittest.TestCase):
    def test_fetch_available_dynon_databases(self):
        dbs = fetch_available_dynon_databases()
        self.assertIsInstance(dbs, list)
        self.assertGreater(len(dbs), 0)

        first = dbs[0]
        self.assertIn("filename", first)
        self.assertTrue(first["filename"].endswith(".duc"))
        self.assertIn("url", first)
        self.assertIn("aviation_validity", first)
        self.assertIn("obstacle_validity", first)
        self.assertIn("label", first)

    def test_default_dir(self):
        self.assertEqual(
            DEFAULT_DYNON_NAV_DIR,
            Path("/Users/GFahmy/Documents/RV-7/Software/sv_software"),
        )


if __name__ == "__main__":
    unittest.main()
