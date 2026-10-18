"""
Unit and Integration Tests for Batch Enhancement Engine (tests/test_batch_engine.py).
"""

import tempfile
import unittest
from pathlib import Path

from enhancer.batch_engine import (
    BatchProgress,
    discover_iac_plates,
    enhance_single_iac_file,
    find_plates_sqlite,
    run_batch_enhancement,
)


class TestBatchEngine(unittest.TestCase):
    def setUp(self):
        self.chart_data_dir = Path("/Users/GFahmy/Documents/RV-7/Airmate/ChartData")

    def test_find_plates_sqlite(self):
        if not self.chart_data_dir.exists():
            self.skipTest("ChartData directory not available")

        # Test from ChartData root
        db1 = find_plates_sqlite(self.chart_data_dir)
        self.assertIsNotNone(db1)
        self.assertTrue(db1.is_file())
        self.assertEqual(db1.name, "Plates.sqlite")

        # Test from ChartData/Plates
        db2 = find_plates_sqlite(self.chart_data_dir / "Plates")
        self.assertIsNotNone(db2)
        self.assertEqual(db2.name, "Plates.sqlite")

        # Test from ChartData/Plates/US
        db3 = find_plates_sqlite(self.chart_data_dir / "Plates" / "US")
        self.assertIsNotNone(db3)
        self.assertEqual(db3.name, "Plates.sqlite")

    def test_discover_iac_plates(self):
        if not self.chart_data_dir.exists():
            self.skipTest("ChartData directory not available")

        plates, info = discover_iac_plates(self.chart_data_dir)
        self.assertGreater(len(plates), 10000, "Should discover >10,000 IAC plates in ChartData")
        self.assertIn("SQLite Database", info)

        # Verify all discovered plates are IAC
        for p in plates[:100]:
            self.assertIn(p.chart_code, ("IAC", "IAP", "DAU"))
            self.assertTrue(p.file_path.is_file(), f"Plate file must exist: {p.file_path}")
            self.assertTrue(p.file_name.upper().endswith(".PDF"))

    def test_enhance_single_and_batch_iac(self):
        if not self.chart_data_dir.exists():
            self.skipTest("ChartData directory not available")

        plates, _ = discover_iac_plates(self.chart_data_dir)
        self.assertTrue(len(plates) > 0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            sample_item = plates[0]
            out_pdf = tmp_path / sample_item.file_name

            # Test PDF enhancement
            res_pdf = enhance_single_iac_file(sample_item, output_path=out_pdf, output_format="PDF")
            self.assertTrue(res_pdf.success)
            self.assertGreater(res_pdf.highlights_count, 0)
            self.assertTrue(out_pdf.is_file())

            # Test PNG enhancement
            out_png = tmp_path / "test_out"
            res_png = enhance_single_iac_file(sample_item, output_path=out_png, output_format="PNG")
            self.assertTrue(res_png.success)
            self.assertTrue((tmp_path / "test_out.PNG").is_file())

            # Now test mini batch of 3 plates with PNG output
            test_subset = plates[:3]
            prog_updates = []

            def on_progress(p: BatchProgress):
                prog_updates.append((p.completed, p.total, p.current_item.file_name))

            batch_results = run_batch_enhancement(
                test_subset,
                output_dir=tmp_path / "png_batch",
                output_format="PNG",
                max_workers=2,
                progress_callback=on_progress,
            )
            self.assertEqual(len(batch_results), 3)
            self.assertTrue(all(r.success for r in batch_results))
            self.assertEqual(len(prog_updates), 3)
            self.assertTrue((tmp_path / "png_batch" / f"{test_subset[0].file_path.stem}.PNG").is_file())


if __name__ == "__main__":
    unittest.main()
