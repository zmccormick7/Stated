from math import exp, isclose, lgamma, nextafter
import unittest

from premisedb import OpenRouterReranker, Reranker


class TransformTests(unittest.TestCase):
    def test_dsrag_beta_cdf_values_endpoints_and_monotonicity(self):
        # Values from scipy.stats.beta.cdf using dsRAG's fixed parameters.
        expected = {
            "cohere/rerank-4-pro": (0.239739158734015, 0.5000000000000001, 0.7602608412659853),
            "voyageai/rerank-2.5": (0.4349888153591312, 0.8614804621007595, 0.9935653164502862),
        }
        for model, reference in expected.items():
            reranker = OpenRouterReranker(model, api_key="test")
            actual = [reranker.transform(s) for s in (0, 0.1, 0.5, 0.9, 1)]
            self.assertEqual(actual[0], 0)
            self.assertEqual(actual[-1], 1)
            self.assertEqual(actual, sorted(actual))
            for result, value in zip(actual[1:-1], reference):
                self.assertAlmostEqual(result, value, places=12)

    def test_jev_and_custom_rerankers_leave_scores_unchanged(self):
        class Custom(Reranker):
            def _score(self, query, segments):
                return [0.5] * len(segments)

        for reranker in (Custom(), OpenRouterReranker("typesafe/jev-1.13", api_key="test")):
            for score in (0, 1e-12, 0.1, 0.5, 0.9, 1):
                self.assertEqual(reranker.transform(score), score)

    def test_extreme_lower_tail_matches_the_power_law_limit(self):
        for model, a, b in (("cohere/rerank-4-pro", 0.4, 0.4), ("voyageai/rerank-2.5", 0.5, 1.8)):
            reranker = OpenRouterReranker(model, api_key="test")
            normalizer = a * exp(lgamma(a) + lgamma(b) - lgamma(a + b))
            for x in (nextafter(0.0, 1.0), 1e-300, 1e-100):
                # At this scale, I_x(a,b) = x**a / (a * B(a,b)) to floating-point precision.
                expected = x ** a / normalizer
                self.assertTrue(isclose(reranker.transform(x), expected, rel_tol=2e-13, abs_tol=0))

    def test_matches_optional_scipy_reference_on_grid_and_tails(self):
        try:
            from scipy.stats import beta
        except ImportError:
            self.skipTest("Install the evaluation extra for the SciPy reference check")
        points = {i / 10000 for i in range(10001)}
        points.update(10.0 ** -i for i in range(1, 324))
        points.update(1 - 10.0 ** -i for i in range(1, 17))
        points.update((nextafter(0.0, 1.0), nextafter(1.0, 0.0)))
        for model, a, b in (("cohere/rerank-4-pro", 0.4, 0.4), ("voyageai/rerank-2.5", 0.5, 1.8)):
            threshold = (a + 1) / (a + b + 2)
            inputs = sorted(points | {threshold, nextafter(threshold, 0), nextafter(threshold, 1)})
            reranker = OpenRouterReranker(model, api_key="test")
            for x, expected in zip(inputs, beta.cdf(inputs, a, b)):
                actual = reranker.transform(x)
                self.assertTrue(0 <= actual <= 1)
                self.assertTrue(isclose(actual, float(expected), rel_tol=2e-13, abs_tol=2e-14), x)

    def test_invalid_raw_scores_are_rejected_by_every_adapter(self):
        for model in OpenRouterReranker.MODELS:
            reranker = OpenRouterReranker(model, api_key="test")
            for score in (True, -1, 2, float("nan"), float("inf"), "0.9"):
                with self.subTest(model=model, score=score), self.assertRaises(ValueError):
                    reranker.transform(score)
