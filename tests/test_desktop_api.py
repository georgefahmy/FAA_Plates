"""
Unit tests for the native desktop API bridge (desktop_api.py).
"""

import unittest
from pathlib import Path

from desktop_api import DesktopAPI


class TestDesktopAPI(unittest.TestCase):
    def setUp(self):
        self.api = DesktopAPI()

    def test_init_app(self):
        init_data = self.api.init_app()
        self.assertIsInstance(init_data, dict)
        self.assertIn("cycle", init_data)
        self.assertIn("available_editions", init_data)
        self.assertIn("faa_dir", init_data)
        self.assertIn("output_dir", init_data)
        self.assertIn("dynon_nav_dir", init_data)
        self.assertIn("dynon_databases", init_data)
        self.assertIsInstance(init_data["dynon_databases"], list)

    def test_search_airports(self):
        matches = self.api.search_airports("E16", limit=5)
        self.assertIsInstance(matches, list)
        if matches:
            first = matches[0]
            self.assertIn("ident", first)
            self.assertIn("id", first)
            self.assertIn("name", first)

    def test_get_airport_charts(self):
        matches = self.api.search_airports("E16", limit=1)
        if matches:
            apt_id = matches[0]["id"]
            charts = self.api.get_airport_charts(apt_id)
            self.assertIsInstance(charts, list)
            if charts:
                first = charts[0]
                self.assertIn("chart_name", first)
                self.assertIn("pdf_path", first)
                self.assertIn("is_approach", first)
                # Approach plates must be sorted first
                self.assertTrue(first["is_approach"])
                self.assertEqual(first["chart_code"], "IAP")

    def test_non_approach_documents_do_not_receive_highlights(self):
        matches = self.api.search_airports("E16", limit=1)
        if not matches:
            return
        apt_id = matches[0]["id"]
        charts = self.api.get_airport_charts(apt_id)

        # 1. Takeoff Minimums (SW2TO.PDF) must render with 0 highlights and is_approach=False
        min_charts = [c for c in charts if c["chart_code"] == "MIN" and c["exists"]]
        if min_charts:
            res_min = self.api.render_plate(min_charts[0]["pdf_path"], chart_code="MIN")
            self.assertTrue(res_min["success"])
            self.assertFalse(res_min["is_approach"])
            self.assertEqual(res_min["highlights_count"], 0)
            self.assertIn("Standard Document", res_min["briefing"])

        # 2. Approach plate (09260R32.PDF) must render with highlights and is_approach=True
        iap_charts = [c for c in charts if c["chart_code"] == "IAP" and c["exists"]]
        if iap_charts:
            res_iap = self.api.render_plate(iap_charts[0]["pdf_path"], chart_code="IAP")
            self.assertTrue(res_iap["success"])
            self.assertTrue(res_iap["is_approach"])
            self.assertGreater(res_iap["highlights_count"], 0)

    def test_pipeline_control_methods(self):
        # When no pipeline is running, control calls return clean error
        self.assertFalse(self.api.pause_pipeline()["success"])
        self.assertFalse(self.api.resume_pipeline()["success"])
        self.assertFalse(self.api.stop_pipeline()["success"])

        # With mock active controller
        from enhancer.pipeline_controller import PipelineController
        self.api._controller = PipelineController()
        self.api._pipeline_running = True

        p_res = self.api.pause_pipeline()
        self.assertTrue(p_res["success"])
        self.assertTrue(p_res["is_paused"])
        self.assertTrue(self.api._controller.is_paused)

        r_res = self.api.resume_pipeline()
        self.assertTrue(r_res["success"])
        self.assertFalse(r_res["is_paused"])
        self.assertFalse(self.api._controller.is_paused)

        s_res = self.api.stop_pipeline()
        self.assertTrue(s_res["success"])
        self.assertTrue(s_res["is_stopped"])
        self.assertTrue(self.api._controller.is_stopped)


if __name__ == "__main__":
    unittest.main()
