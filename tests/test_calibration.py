from dataclasses import replace
import unittest

from premisedb.calibration import ScoreCalibration, calibration_for


class CalibrationTests(unittest.TestCase):
    def test_named_profiles_are_model_specific_monotone_and_leave_raw_scores_untouched(self):
        raw = [0, 0.1, 0.5, 0.9, 1]
        for model in ("typesafe/jev-1.13", "cohere/rerank-4-pro", "voyageai/rerank-2.5"):
            profile = calibration_for(model)
            calibrated = profile.apply(raw)
            self.assertEqual(profile.model, model)
            self.assertEqual(tuple(sorted(calibrated)), calibrated)
            self.assertTrue(all(0 <= p <= 1 for p in calibrated))
        self.assertEqual(raw, [0, 0.1, 0.5, 0.9, 1])
        with self.assertRaises(ValueError):
            calibration_for("unknown")

    def test_invalid_profiles_and_scores_fail_and_extreme_sigmoids_are_stable(self):
        profile = ScoreCalibration("fixture", "fixture", 1, 0)
        for changes in ({"name": ""}, {"model": ""}, {"slope": 0}, {"slope": -1},
                        {"slope": True}, {"intercept": float("nan")}, {"minimum_value": -1}):
            with self.assertRaises(ValueError):
                replace(profile, **changes)
        for score in (True, -1, 2, float("nan"), float("inf"), "0.9"):
            with self.assertRaises(ValueError):
                profile.apply([score])
        self.assertEqual(replace(profile, slope=1e308).apply([0, 1]), (0, 1))
