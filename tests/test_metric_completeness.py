"""Missing measurements must never appear as zero emissions or throughput."""
import unittest
from src.tools.evaluator import PerformanceEvaluator


class MetricCompletenessTests(unittest.TestCase):
    def test_missing_and_invalid_totals_are_unknown(self):
        for value in (None, float('nan'), float('inf'), -1, 'invalid'):
            with self.subTest(value=value):
                result = PerformanceEvaluator.compute_summary_kpi({
                    'total_co2_mg': value, 'total_fuel_mg': value,
                    'completed_trips': value, 'simulation_duration': 600})
                for key in ('co2_emissions_kg', 'fuel_liters', 'throughput_vph'):
                    self.assertIsNone(result[key])

    def test_measured_zero_remains_zero(self):
        result = PerformanceEvaluator.compute_summary_kpi({
            'total_co2_mg': 0, 'total_fuel_mg': 0,
            'completed_trips': 0, 'simulation_duration': 600})
        for key in ('co2_emissions_kg', 'fuel_liters', 'throughput_vph'):
            self.assertEqual(result[key], 0)

    def test_rate_requires_valid_duration(self):
        for duration in (None, 0, -1, float('nan')):
            with self.subTest(duration=duration):
                result = PerformanceEvaluator.compute_summary_kpi({
                    'completed_trips': 10, 'simulation_duration': duration})
                self.assertIsNone(result['throughput_vph'])
