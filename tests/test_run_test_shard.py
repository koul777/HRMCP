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


if __name__ == "__main__":
    unittest.main()
