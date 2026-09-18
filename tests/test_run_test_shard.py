import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_test_shard", ROOT / "scripts" / "run_test_shard.py")
run_test_shard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_test_shard)


def test_ids(suite):
    return [test.id() for test in run_test_shard.iter_tests(suite)]


class RunTestShardTests(unittest.TestCase):
    def test_shards_partition_the_whole_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(7):
                Path(tmp, f"test_sample_{index}.py").write_text(
                    "import unittest\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_a(self): pass\n"
                    "    def test_b(self): pass\n",
                    encoding="utf-8",
                )
            Path(tmp, "test_broken.py").write_text("import not_a_real_module\n", encoding="utf-8")
            suite = unittest.TestLoader().discover(tmp, top_level_dir=tmp)
            everything = test_ids(suite)
            for count in (1, 2, 3):
                with self.subTest(count=count):
                    shards = [test_ids(run_test_shard.select_shard(suite, i, count)) for i in range(count)]
                    combined = [test for shard in shards for test in shard]
                    self.assertCountEqual(combined, everything)
                    self.assertEqual(len(combined), len(set(combined)))
                    self.assertTrue(any("test_broken" in test for test in shards[0]))

    def test_rejects_out_of_range_index(self):
        with self.assertRaises(SystemExit):
            run_test_shard.main(["--index", "3", "--count", "3"])


class ShardBalanceTests(unittest.TestCase):
    def test_recorded_weights_drive_the_split(self) -> None:
        modules = ["slow", "medium", "fast_a", "fast_b"]
        weights = {"slow": 100.0, "medium": 40.0, "fast_a": 5.0, "fast_b": 5.0}
        assignment = run_test_shard.assign_modules(modules, 2, weights)
        loads = [0.0, 0.0]
        for name in modules:
            loads[assignment[name]] += weights[name]
        # Longest-first packing keeps the slowest module alone rather than
        # stacking it with the next heaviest.
        self.assertEqual(sorted(loads), [50.0, 100.0])

    def test_unknown_modules_are_not_treated_as_free(self) -> None:
        modules = ["known", "new_one", "new_two"]
        assignment = run_test_shard.assign_modules(modules, 2, {"known": 1.0})
        self.assertNotEqual(assignment["new_one"], assignment["new_two"])

    def test_assignment_is_deterministic_and_covers_every_module(self) -> None:
        modules = [f"test_module_{index}" for index in range(12)]
        weights = {name: float(index) for index, name in enumerate(modules)}
        first = run_test_shard.assign_modules(modules, 3, weights)
        second = run_test_shard.assign_modules(list(reversed(modules)), 3, weights)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(modules))
        self.assertTrue(all(0 <= shard < 3 for shard in first.values()))

    def test_missing_or_broken_weight_file_falls_back_quietly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp, "absent.json")
            self.assertEqual(run_test_shard.load_weights(missing), {})
            broken = Path(tmp, "broken.json")
            broken.write_text("not json", encoding="utf-8")
            self.assertEqual(run_test_shard.load_weights(broken), {})
            wrong_shape = Path(tmp, "wrong.json")
            wrong_shape.write_text('{"weights": []}', encoding="utf-8")
            self.assertEqual(run_test_shard.load_weights(wrong_shape), {})

    def test_committed_weights_cover_the_slowest_modules(self) -> None:
        weights = run_test_shard.load_weights()
        self.assertGreater(len(weights), 50)
        self.assertTrue(all(value > 0 for value in weights.values()))
        heaviest = max(weights, key=weights.get)
        self.assertTrue(
            (ROOT / "tests" / f"{heaviest}.py").is_file(),
            f"recorded weight for a module that no longer exists: {heaviest}",
        )


if __name__ == "__main__":
    unittest.main()
