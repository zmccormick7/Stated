from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class ExpandTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = RecordStore(Path(directory.name) / "research.sqlite")
        self.addCleanup(self.db.close)
        make = lambda kind, text: self.db.create(kind, text=text, author_id="a")
        self.entity, self.finding, self.variable, self.question, self.other = (
            make("Entity", "PowerFrame"), make("Finding", "Backlog may slip"), make("Variable", "FY28 revenue"),
            make("Question", "Can customers defer?"), make("Finding", "Grid delays"))
        link = lambda a, b, relation: self.db.create_link(a.id, b.id, relation, author_id="a")
        self.about = link(self.finding, self.entity, "about")
        self.depends = link(self.variable, self.finding, "depends_on")
        self.raises = link(self.finding, self.question, "raises")
        self.other_about = link(self.other, self.entity, "about")

    def summary(self, expansion):
        return [(r.record.text, r.depth, tuple(link.relation for link in r.path)) for r in expansion.reached]

    def test_breadth_first_walk_reports_depth_and_path(self):
        one = self.db.expand([self.entity.id])
        self.assertEqual(self.summary(one), [("PowerFrame", 0, ()), ("Backlog may slip", 1, ("about",)),
                                             ("Grid delays", 1, ("about",))])
        two = self.db.expand([self.entity.id], depth=2)
        self.assertEqual(self.summary(two)[3:], [("FY28 revenue", 2, ("about", "depends_on")),
                                                 ("Can customers defer?", 2, ("about", "raises"))])
        self.assertEqual(two.reached[3].path, (self.about, self.depends))
        self.assertEqual((two.capped, two.has_more), ((), False))

    def test_direction_relations_and_multiple_seeds(self):
        incoming = self.db.expand([self.entity.id], depth=2, direction="in")
        self.assertEqual([r.record.text for r in incoming.reached], ["PowerFrame", "Backlog may slip", "Grid delays",
                                                                     "FY28 revenue"])
        filtered = self.db.expand([self.entity.id], depth=2, relations=["about", "raises"])
        self.assertNotIn("FY28 revenue", [r.record.text for r in filtered.reached])
        seeds = self.db.expand([self.question.id, self.variable.id, self.question.id])
        self.assertEqual(self.summary(seeds), [("Can customers defer?", 0, ()), ("FY28 revenue", 0, ()),
                                               ("Backlog may slip", 1, ("raises",))])

    def test_hub_cap_and_overall_limit(self):
        capped = self.db.expand([self.entity.id], depth=2, per_record_limit=1)
        self.assertEqual([r.record.text for r in capped.reached], ["PowerFrame", "Backlog may slip", "FY28 revenue"])
        self.assertEqual(capped.capped, (self.entity.id, self.finding.id))
        limited = self.db.expand([self.entity.id], depth=2, limit=3)
        self.assertEqual((len(limited.reached), limited.has_more), (4, True))
        self.assertFalse(self.db.expand([self.entity.id], depth=2, limit=4).has_more)

    def test_invalid_walks(self):
        for kwargs in ({"depth": 0}, {"depth": 3}, {"direction": "up"}, {"relations": "about"}, {"relations": []},
                       {"relations": [" "]}, {"limit": 0}, {"per_record_limit": 1001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.db.expand([self.entity.id], **kwargs)
        for seeds in ("abc", [], [3], [str(n) for n in range(101)]):
            with self.subTest(seeds=str(seeds)[:20]), self.assertRaises(ValueError):
                self.db.expand(seeds)
        with self.assertRaises(KeyError):
            self.db.expand([self.entity.id, "missing"])


if __name__ == "__main__":
    unittest.main()
