"""Offline tests for the small-model public MCP chaining evaluator."""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from scripts import eval_small_model_tool_chaining as evaluation


@contextmanager
def temporary_directory():
    """Use inherited Windows ACLs; Python 3.13's mode 0700 blocks the sandbox."""
    root = evaluation.ROOT.resolve()
    path = root / f".test-tool-chaining-{uuid4().hex}"
    path.mkdir(mode=0o777)
    try:
        yield path
    finally:
        if path.resolve().parent != root:
            raise RuntimeError("Temporary directory escaped the workspace")
        shutil.rmtree(path)


def make_case(case_id: str = "elements-u1") -> dict:
    return {
        "id": case_id,
        "template": "elements",
        "question": "'인사기획' 능력단위의 능력단위요소를 모두 알려줘.",
        "expected_terms": ["인력 계획", "채용ㆍ배치", "성과 분석"],
        "pass_rule": "at_least_half",
    }


def make_tools() -> list[dict]:
    return evaluation.ollama_tools([
        SimpleNamespace(name=name, description=f"Description of {name}",
                        inputSchema={"type": "object", "properties": {}})
        for name in ("ncs_search", "ncs_unit_detail")
    ])


def text_blocks(text: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(type="text", text=text)]


def answer_reply(text: str, calls: list | None = None) -> dict:
    message = {"role": "assistant", "content": text}
    if calls is not None:
        message["tool_calls"] = calls
    return {"message": message, "prompt_eval_count": 10, "eval_count": 3}


def tool_call(name: str, arguments: object = None) -> dict:
    return {"function": {"name": name, "arguments": {} if arguments is None else arguments}}


class FakeHTTP:
    """Record immutable request snapshots and serve predetermined replies."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict | None]] = []

    def __call__(self, url: str, payload: dict | None) -> dict:
        self.requests.append((url, deepcopy(payload)))
        if url.endswith("/api/version"):
            if payload is not None:
                raise AssertionError("Version must use GET")
            return {"version": "offline-test-version"}
        if not url.endswith("/api/chat") or payload is None:
            raise AssertionError("Unexpected HTTP request")
        return self.replies.pop(0)

    @property
    def chats(self) -> list[dict]:
        return [payload for url, payload in self.requests if url.endswith("/api/chat")]


class ChainingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.network = patch.object(evaluation, "urlopen", side_effect=AssertionError("No network allowed"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def evaluate(self, http: FakeHTTP, caller: AsyncMock, **kwargs: object) -> dict:
        return asyncio.run(evaluation.evaluate_case(
            make_case(), make_tools(), http_fn=http, tool_caller=caller, **kwargs,
        ))

    def test_tool_call_loop_then_final_answer(self) -> None:
        http = FakeHTTP([
            answer_reply("검색합니다.", [tool_call("ncs_search", {"query": "인사기획"})]),
            answer_reply("", [tool_call("ncs_unit_detail", {"unit_code": "u1"})]),
            answer_reply("인력 계획과 채용·배치가 있습니다."),
        ])
        caller = AsyncMock(side_effect=[
            text_blocks("unit_code: u1"),
            (text_blocks("인력 계획, 채용ㆍ배치, 성과 분석"), {"private_copy": "not sent"}),
        ])
        result = self.evaluate(http, caller)
        self.assertTrue(result["passed"])
        self.assertEqual(result["tool_calls"], ["ncs_search", "ncs_unit_detail"])
        self.assertTrue(result["ncs_unit_detail_called"])
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["stop_reason"], "answered")
        self.assertEqual(result["prompt_eval_count"], 30)
        self.assertEqual(result["eval_count"], 9)
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        caller.assert_any_await("ncs_unit_detail", {"unit_code": "u1"})
        first = http.chats[0]
        self.assertEqual(first["messages"], [
            {"role": "system", "content": evaluation.SYSTEM_PROMPT},
            {"role": "user", "content": make_case()["question"]},
        ])
        self.assertFalse(first["think"])
        self.assertFalse(first["stream"])
        self.assertEqual(first["options"], {"temperature": 0, "seed": 20261024, "num_predict": 2048, "num_ctx": 12288})
        self.assertEqual(first["tools"], make_tools())
        last_messages = http.chats[-1]["messages"]
        self.assertEqual([message["role"] for message in last_messages],
                         ["system", "user", "assistant", "tool", "assistant", "tool"])
        self.assertEqual(last_messages[-1], {
            "role": "tool", "content": "인력 계획, 채용ㆍ배치, 성과 분석", "tool_name": "ncs_unit_detail",
        })

    def test_max_turns_does_not_score_intermediate_assistant_text(self) -> None:
        http = FakeHTTP([
            answer_reply("인력 계획, 채용ㆍ배치", [tool_call("ncs_search")]),
            answer_reply("인력 계획, 채용ㆍ배치", [tool_call("ncs_search")]),
        ])
        caller = AsyncMock(return_value=text_blocks("인력 계획, 채용ㆍ배치"))
        result = self.evaluate(http, caller, max_turns=2)
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["stop_reason"], "max_turns")
        self.assertEqual(result["final_answer"], "")
        self.assertFalse(result["passed"])
        self.assertEqual(caller.await_count, 2)

    def test_unknown_tool_is_rejected_and_model_can_continue(self) -> None:
        http = FakeHTTP([
            answer_reply("", [tool_call("review_ontology_concept")]),
            answer_reply("정보가 없습니다."),
        ])
        caller = AsyncMock()
        result = self.evaluate(http, caller)
        caller.assert_not_awaited()
        self.assertEqual(result["tool_calls"], ["review_ontology_concept"])
        self.assertEqual(result["tool_results"][0]["error"], "unknown_public_tool")
        self.assertIn("Tool error", http.chats[-1]["messages"][-1]["content"])
        self.assertTrue(result["false_no_data"])

    def test_multiple_calls_are_executed_in_order_and_truncated(self) -> None:
        http = FakeHTTP([
            answer_reply("", [tool_call("ncs_search"), tool_call("ncs_unit_detail")]),
            answer_reply("인력계획, 채용·배치"),
        ])
        caller = AsyncMock(side_effect=[text_blocks("a" * 6001), text_blocks("b" * 6000)])
        result = self.evaluate(http, caller)
        self.assertEqual(result["tool_calls"], ["ncs_search", "ncs_unit_detail"])
        self.assertEqual([row["truncated"] for row in result["tool_results"]], [True, False])
        self.assertEqual(result["tool_results"][0]["original_chars"], 6001)
        self.assertEqual(result["tool_results"][0]["returned_chars"], 6000)
        messages = http.chats[-1]["messages"]
        self.assertEqual(messages[-2]["content"], "a" * 6000)
        self.assertEqual(messages[-1]["content"], "b" * 6000)

    def test_string_arguments_and_tool_exception_are_handled(self) -> None:
        http = FakeHTTP([
            answer_reply("", [tool_call("ncs_search", '{"query":"test"}')]),
            answer_reply("확인되지 않습니다."),
        ])
        caller = AsyncMock(side_effect=RuntimeError("DO_NOT_ECHO_SECRET"))
        result = self.evaluate(http, caller)
        caller.assert_awaited_once_with("ncs_search", {"query": "test"})
        self.assertEqual(result["tool_results"][0]["error"], "RuntimeError")
        self.assertNotIn("DO_NOT_ECHO_SECRET", json.dumps(http.requests))
        self.assertNotIn("DO_NOT_ECHO_SECRET", json.dumps(result))
        self.assertEqual(result["stop_reason"], "answered")

    def test_invalid_arguments_never_reach_tool_caller(self) -> None:
        http = FakeHTTP([
            answer_reply("", [tool_call("ncs_search", "[1,2]")]),
            answer_reply("다시 시도해야 합니다."),
        ])
        caller = AsyncMock()
        result = self.evaluate(http, caller)
        caller.assert_not_awaited()
        self.assertEqual(result["tool_results"][0]["error"], "ValueError")

    def test_http_error_is_a_case_result_and_preserves_previous_tokens(self) -> None:
        calls = 0

        def failing_http(url: str, payload: dict | None) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                return answer_reply("", [tool_call("ncs_search")])
            raise OSError("DO_NOT_ECHO_SECRET")

        result = asyncio.run(evaluation.evaluate_case(
            make_case(), make_tools(), http_fn=failing_http,
            tool_caller=AsyncMock(return_value=text_blocks("found")),
        ))
        self.assertEqual(result["stop_reason"], "error")
        self.assertEqual(result["error"], "OSError")
        self.assertEqual(result["prompt_eval_count"], 10)
        self.assertEqual(result["turns"], 2)
        self.assertFalse(result["passed"])
        self.assertNotIn("DO_NOT_ECHO_SECRET", json.dumps(result))

    def test_tool_result_joiner_supports_installed_mcp_shapes(self) -> None:
        blocks = [*text_blocks("first"), SimpleNamespace(type="image", data="ignored"),
                  {"type": "text", "text": "second"}]
        for result in (blocks, (blocks, {"answer": "not duplicated"}),
                       SimpleNamespace(content=blocks), {"content": blocks}):
            with self.subTest(shape=type(result).__name__):
                self.assertEqual(evaluation.tool_result_text(result), "first\nsecond")


class ScoringTests(unittest.TestCase):
    def test_half_threshold_rounds_up_and_all_requires_both_names(self) -> None:
        case = make_case()
        self.assertFalse(evaluation.score_answer(case, "인력 계획")["passed"])
        self.assertTrue(evaluation.score_answer(case, "인력 계획, 성과 분석")["passed"])
        case["expected_terms"] = ["경영·회계·사무", "인사"]
        case["pass_rule"] = "all"
        self.assertFalse(evaluation.score_answer(case, "인사")["passed"])
        self.assertTrue(evaluation.score_answer(case, "경영·회계·사무 대분류, 인사 세분류")["passed"])
        case["pass_rule"] = "at_least_half"
        self.assertTrue(evaluation.score_answer(case, "인사")["passed"])

    def test_false_no_data_only_when_failed(self) -> None:
        for phrase in ("찾을 수 없습니다", "없습니다", "존재하지 않습니다",
                       "확인되지 않습니다", "정보가 없어요"):
            with self.subTest(phrase=phrase):
                self.assertTrue(evaluation.score_answer(make_case(), phrase)["false_no_data"])
        result = evaluation.score_answer(make_case(), "인력계획, 채용·배치입니다. 다른 정보는 없습니다.")
        self.assertTrue(result["passed"])
        self.assertFalse(result["false_no_data"])
        self.assertFalse(evaluation.score_answer(make_case(), "다시 검색하겠습니다.")["false_no_data"])

    def test_whitespace_and_middle_dot_normalization(self) -> None:
        self.assertEqual(evaluation.normalize_text(" 채용 ㆍ 배치\n\t"), "채용·배치")
        result = evaluation.score_answer(make_case(), "인력\n계획, 채용 · 배치")
        self.assertEqual(result["matched_expected_terms"], ["인력 계획", "채용ㆍ배치"])
        self.assertTrue(result["passed"])

    def test_wilson_interval_and_summary_denominators(self) -> None:
        low, high = evaluation.wilson_interval(1, 2)
        self.assertAlmostEqual(low, 0.0945312, places=6)
        self.assertAlmostEqual(high, 0.9054688, places=6)
        self.assertEqual(evaluation.wilson_interval(0, 0), [None, None])
        self.assertAlmostEqual(evaluation.wilson_interval(0, 1)[0], 0)
        self.assertAlmostEqual(evaluation.wilson_interval(1, 1)[1], 1)
        rows = [
            {"template": "elements", "passed": True, "false_no_data": False,
             "stop_reason": "answered", "tool_calls": ["ncs_search", "ncs_unit_detail"],
             "prompt_eval_count": 20, "eval_count": 5, "elapsed_seconds": 2.5},
            {"template": "classification", "passed": False, "false_no_data": True,
             "stop_reason": "answered", "tool_calls": ["ncs_search"],
             "prompt_eval_count": 10, "eval_count": 3, "elapsed_seconds": 1.5},
        ]
        summary = evaluation.summarize(rows)
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["false_no_data_rate"], 0.5)
        self.assertEqual(summary["mean_tool_calls"], 1.5)
        self.assertEqual(summary["per_template"]["classification"]["pass_rate"], 0)
        self.assertEqual(summary["totals"], {"prompt_eval_count": 30, "eval_count": 8, "elapsed_seconds": 4,
                                             "length_stopped_replies": 0, "max_request_prompt_tokens": 0})
        self.assertEqual(len(summary["top_tool_call_sequences"]), 2)


class BuildCasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = self.enterContext(temporary_directory())
        self.db_path = self.temp / "sample #한글.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
                    middle_name TEXT, small_name TEXT, sub_name TEXT
                );
                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY, unit_name_raw TEXT, classification_id INTEGER
                );
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY, unit_code TEXT, element_no TEXT, element_name_raw TEXT
                );
            """)
            conn.executemany("INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?)", [
                (1, "02", "경영·회계·사무", "경영", "총무", "인사"),
                (2, "20", "정보통신", "정보기술", "개발", "응용개발"),
            ])
            conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?)", [
                ("u2", "채용관리", 1), ("u1", "인사기획", 1),
                ("v2", "구현", 2), ("v1", "설계", 2),
                ("d1", "중복이름", 1), ("d2", "중복이름", 2),
                ("empty", "요소없음", 2), ("blank", "빈요소", 2),
            ])
            conn.executemany("INSERT INTO competency_elements VALUES (?, ?, ?, ?)", [
                (2, "u1", "2", "채용ㆍ배치"), (1, "u1", "1", "인력 계획"),
                (3, "u2", "1", "채용 계획"), (4, "v1", "1", "설계 검토"),
                (5, "v2", "1", "기능 구현"), (6, "d1", "1", "중복 요소"),
                # d2 has no elements, but its name still makes d1 ambiguous.
                (7, "blank", "1", ""),
            ])
            conn.commit()

    def test_deterministic_stratified_sampling_and_fingerprint(self) -> None:
        before = self.db_path.read_bytes()
        mtime = self.db_path.stat().st_mtime_ns
        first = evaluation.build_cases(self.db_path, per_major=1)
        second = evaluation.build_cases(self.db_path, per_major=1)
        self.assertEqual(first["cases"], second["cases"])
        self.assertEqual(first["cases_sha256"], second["cases_sha256"])
        self.assertEqual(first["cases_sha256"], evaluation.sha256_json(first["cases"]))
        self.assertEqual(len(first["cases"]), 4)
        self.assertEqual([case["major_code"] for case in first["cases"]], ["02", "02", "20", "20"])
        self.assertEqual(first["table_row_counts"], {
            "classifications": 2, "competency_units": 8, "competency_elements": 7,
        })
        self.assertEqual(first["db"]["size_bytes"], len(before))
        self.assertEqual(first["db"]["mtime_ns"], mtime)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(self.db_path.stat().st_mtime_ns, mtime)
        evaluation.validate_cases(first)

    def test_ambiguity_and_empty_elements_are_skipped_before_sampling(self) -> None:
        result = evaluation.build_cases(self.db_path, per_major=99)
        self.assertEqual({case["unit_code"] for case in result["cases"]}, {"u1", "u2", "v1", "v2"})
        by_id = {case["id"]: case for case in result["cases"]}
        self.assertEqual(by_id["elements-u1"]["expected_terms"], ["인력 계획", "채용ㆍ배치"])
        self.assertEqual(by_id["elements-u1"]["pass_rule"], "at_least_half")
        self.assertEqual(by_id["classification-u1"]["expected_terms"], ["경영·회계·사무", "인사"])
        self.assertEqual(by_id["classification-u1"]["pass_rule"], "all")
        self.assertEqual(by_id["classification-u1"]["question"],
                         "'인사기획' 능력단위는 NCS 분류체계에서 어느 대분류와 세분류에 속해?")

    def test_connection_really_uses_sqlite_read_only_uri(self) -> None:
        original = sqlite3.connect
        observations = []

        def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            conn = original(*args, **kwargs)
            try:
                conn.execute("CREATE TABLE forbidden_probe (value TEXT)")
            except sqlite3.OperationalError as exc:
                observations.append(str(exc))
            else:
                self.fail("The case builder opened a writable database")
            return conn

        with patch.object(evaluation.sqlite3, "connect", side_effect=connect):
            evaluation.build_cases(self.db_path)
        self.assertTrue(observations)
        self.assertIn("readonly", observations[0])

    def test_missing_db_is_not_created_and_changed_case_hash_is_rejected(self) -> None:
        missing = self.db_path.with_name("missing.db")
        with self.assertRaises(FileNotFoundError):
            evaluation.build_cases(missing)
        self.assertFalse(missing.exists())
        result = evaluation.build_cases(self.db_path)
        result["cases"][0]["question"] = "changed"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            evaluation.validate_cases(result)

    def test_build_cli_writes_case_artifact(self) -> None:
        out = self.db_path.with_suffix(".json")
        with patch("sys.stdout", new_callable=io.StringIO):
            code = evaluation.main(["build-cases", "--db", str(self.db_path), "--out", str(out),
                                    "--per-major", "1", "--seed", "20261024"])
        self.assertEqual(code, 0)
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(result["schema"], evaluation.CASES_SCHEMA)
        evaluation.validate_cases(result)


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.enterContext(temporary_directory())
        self.out = self.root / "result.json"
        self.cases_path = self.root / "cases.json"
        cases = [make_case(), make_case("elements-u2")]
        self.document = {
            "schema": evaluation.CASES_SCHEMA, "cases": cases,
            "cases_sha256": evaluation.sha256_json(cases),
            "db": {"path": str(self.root / "sample.db"), "size_bytes": 0, "mtime_ns": 0},
        }
        self.network = patch.object(evaluation, "urlopen", side_effect=AssertionError("No network allowed"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_eval(self, http: FakeHTTP, **kwargs: object) -> dict:
        return asyncio.run(evaluation.run_evaluation(
            self.document, self.cases_path, self.out, make_tools(),
            tool_caller=AsyncMock(), http_fn=http, **kwargs,
        ))

    def records(self) -> list[dict]:
        return [json.loads(line) for line in Path(str(self.out) + ".cases.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_resume_skips_completed_ids_and_includes_them_in_summary(self) -> None:
        first = self.run_eval(FakeHTTP([answer_reply("인력계획, 채용·배치")]), limit=1)
        self.assertEqual(first["counts"]["evaluated"], 1)
        http = FakeHTTP([answer_reply("정보가 없습니다.")])
        resumed = self.run_eval(http)
        self.assertEqual(len(http.chats), 1)
        self.assertEqual(resumed["counts"]["resumed"], 1)
        self.assertEqual(resumed["counts"]["newly_evaluated"], 1)
        self.assertEqual(resumed["counts"]["evaluated"], 2)
        self.assertEqual(resumed["pass_rate"], 0.5)
        self.assertEqual(resumed["false_no_data_rate"], 0.5)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(resumed["tool_definitions_sha256"], evaluation.sha256_json(make_tools()))
        self.assertEqual(resumed["cases_sha256"], self.document["cases_sha256"])
        self.assertEqual(resumed["ollama_version"], "offline-test-version")
        saved = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(saved, resumed)
        no_chats = FakeHTTP([])
        again = self.run_eval(no_chats)
        self.assertEqual(no_chats.chats, [])
        self.assertEqual(again["counts"]["resumed"], 2)
        self.assertEqual(again["totals"], resumed["totals"])

    def test_fresh_replaces_log_and_evaluates_again(self) -> None:
        self.run_eval(FakeHTTP([answer_reply("정보가 없습니다.")] * 2))
        fresh = self.run_eval(FakeHTTP([answer_reply("인력계획, 채용·배치")]), fresh=True, limit=1)
        self.assertEqual(fresh["counts"]["resumed"], 0)
        self.assertEqual(fresh["pass_rate"], 1)
        self.assertEqual(len(self.records()), 1)

    def test_resume_rejects_different_model_without_appending(self) -> None:
        self.run_eval(FakeHTTP([answer_reply("정보가 없습니다.")]), limit=1)
        before = self.records()
        http = FakeHTTP([])
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.run_eval(http, model="different-model")
        self.assertEqual(http.chats, [])
        self.assertEqual(self.records(), before)

    def test_complete_record_without_final_newline_can_resume(self) -> None:
        self.run_eval(FakeHTTP([answer_reply("정보가 없습니다.")]), limit=1)
        log = Path(str(self.out) + ".cases.jsonl")
        log.write_bytes(log.read_bytes().rstrip(b"\r\n"))
        self.run_eval(FakeHTTP([answer_reply("인력계획, 채용·배치")]))
        self.assertEqual(len(self.records()), 2)

    def test_failed_case_does_not_abort_remaining_cases(self) -> None:
        http = FakeHTTP([{"malformed": True}, answer_reply("인력계획, 채용·배치")])
        summary = self.run_eval(http)
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(summary["counts"]["evaluated"], 2)
        self.assertEqual(self.records()[0]["stop_reason"], "error")


class MetadataAndCLITests(unittest.TestCase):
    def test_git_head_loose_detached_and_packed(self) -> None:
        with temporary_directory() as root:
            git = root / ".git"
            (git / "refs/heads").mkdir(parents=True)
            head = "a" * 40
            (git / "HEAD").write_text(head, encoding="utf-8")
            self.assertEqual(evaluation.read_git_head(root), head)
            (git / "HEAD").write_text("ref: refs/heads/test\n", encoding="utf-8")
            (git / "refs/heads/test").write_text(head, encoding="utf-8")
            self.assertEqual(evaluation.read_git_head(root), head)
            (git / "refs/heads/test").unlink()
            (git / "packed-refs").write_text(f"{head} refs/heads/test\n", encoding="utf-8")
            self.assertEqual(evaluation.read_git_head(root), head)

    def test_cli_setup_error_is_nonzero_and_does_not_echo_exception(self) -> None:
        with patch.object(evaluation, "build_cases", side_effect=ValueError("DO_NOT_ECHO_SECRET")):
            with patch("sys.stderr", new_callable=io.StringIO) as output:
                code = evaluation.main(["build-cases", "--out", "unused.json"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("DO_NOT_ECHO_SECRET", output.getvalue())

    def test_cli_completed_evaluation_returns_zero_even_when_cases_fail(self) -> None:
        document = {"schema": evaluation.CASES_SCHEMA, "cases": [make_case()],
                    "cases_sha256": evaluation.sha256_json([make_case()])}
        with patch.object(Path, "read_text", return_value=json.dumps(document)):
            with patch.object(evaluation, "load_public_runtime",
                              return_value=(make_tools(), AsyncMock(), {"server_module_sha256": "x"})):
                with patch.object(evaluation, "run_evaluation", new_callable=AsyncMock,
                                  return_value={"counts": {"passed": 0, "failed": 1}}):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        code = evaluation.main(["run", "--cases", "unused.json", "--out", "unused-result.json"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
