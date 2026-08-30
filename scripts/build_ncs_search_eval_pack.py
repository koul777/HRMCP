from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import statistics
import time
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_OUT = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_search_eval_candidates_20260830.md"
SCHEMA = "ncs_search_eval_candidate_pack_v1"

SearchFunction = Callable[[str, str, int], dict[str, Any]]


def _candidate(
    case_id: str,
    query: str,
    *,
    group: str,
    intent: str,
    scope: str,
    preferred_types: tuple[str, ...],
    tags: tuple[str, ...],
    challenge: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "query": query,
        "domain_group": group,
        "intent_candidate": intent,
        "scope_candidate": scope,
        "preferred_result_type_candidates": list(preferred_types),
        "tags": list(tags),
        "challenge_reason": challenge,
        "evaluation_status": "candidate_eval",
        "gold_label_present": False,
        "requires_human_review": True,
        "human_decision_present": False,
        "human_label_template": {
            "relevance_grade_0_to_3": None,
            "off_scope": None,
            "acceptable_result_ids": [],
            "reviewer_id": None,
            "reviewed_at": None,
            "notes": "",
        },
    }


CANDIDATES: tuple[dict[str, Any], ...] = (
    _candidate("NCS-EVAL-001", "채용", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable"), challenge="두 음절 핵심어로서 trigram 전용 검색에서 누락될 수 있다."),
    _candidate("NCS-EVAL-002", "신입사원 채용 면접", group="hr_core", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "natural_language", "multi_token"), challenge="복수 토큰이 한 문자열에 연속으로 존재하지 않을 가능성이 높다."),
    _candidate("NCS-EVAL-003", "직무기술서 작성", group="hr_core", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "natural_language", "multi_token"), challenge="산출물과 수행 행위가 서로 다른 계층에 나타날 수 있다."),
    _candidate("NCS-EVAL-004", "인사평가", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="결합 명칭과 분리 표기 사이의 일치 여부를 확인해야 한다."),
    _candidate("NCS-EVAL-005", "성과관리", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="여러 직무 범위에서 쓰이는 용어라 범용 결과 과다가 가능하다."),
    _candidate("NCS-EVAL-006", "보상관리", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="임금·급여·복리후생 등 인접 용어와의 경계를 검토해야 한다."),
    _candidate("NCS-EVAL-007", "임금관리", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="법규 지식과 운영 과업이 여러 결과 유형에 분산될 수 있다."),
    _candidate("NCS-EVAL-008", "복리후생", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="과정·제도·운영 행위가 동일 명칭으로 혼재할 수 있다."),
    _candidate("NCS-EVAL-009", "노무관리", group="hr_core", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="광범위한 하위 과업 때문에 상위 결과의 특이도 검토가 필요하다."),
    _candidate("NCS-EVAL-010", "단체교섭", group="hr_core", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="법률 지식과 협상 기술이 함께 검색되는지 살펴야 한다."),
    _candidate("NCS-EVAL-011", "교육훈련", group="hr_core", intent="education_system_design", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "compound_term"), challenge="매우 범용적인 교육 용어로 비HR 결과가 상위에 올 수 있다."),
    _candidate("NCS-EVAL-012", "인재개발", group="hr_core", intent="education_system_design", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="NCS 원문이 인적자원개발 등 다른 명칭을 사용할 수 있다."),
    _candidate("NCS-EVAL-013", "경력개발", group="hr_core", intent="education_system_design", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="경력개발경로와 능력단위 검색 결과를 구분해 해석해야 한다."),
    _candidate("NCS-EVAL-014", "조직문화", group="hr_core", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="추상 개념과 구체적 수행준거의 연결 여부가 불확실하다."),
    _candidate("NCS-EVAL-015", "인력운영", group="hr_core", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "domain_term"), challenge="배치·이동·계획 등 여러 과업 표현으로 분산될 수 있다."),
    _candidate("NCS-EVAL-016", "노동관계법 지식", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "knowledge", "multi_token"), challenge="KSA 원문이 법률별 세부 명칭으로만 존재할 수 있다."),
    _candidate("NCS-EVAL-017", "근로기준법에 대한 지식", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "knowledge", "natural_language"), challenge="조사와 서술형 표현이 포함된 직접 KSA 질의다."),
    _candidate("NCS-EVAL-018", "면접 질문 설계 능력", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "skill", "natural_language"), challenge="행위·산출물·능력 표현이 원문에서 서로 떨어질 수 있다."),
    _candidate("NCS-EVAL-019", "채용 전형 운영 기술", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "skill", "multi_token"), challenge="채용 단계별 KSA가 한 문장에 모두 없을 가능성이 있다."),
    _candidate("NCS-EVAL-020", "인사 데이터 분석 능력", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "skill", "natural_language"), challenge="인사 도메인과 일반 데이터 분석 KSA의 범위를 구분해야 한다."),
    _candidate("NCS-EVAL-021", "평가 결과 피드백 기술", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "skill", "multi_token"), challenge="평가와 피드백이 별도 원자 KSA로 분해됐을 수 있다."),
    _candidate("NCS-EVAL-022", "교육 요구분석 능력", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "skill", "compound_term"), challenge="교육 요구와 요구분석 표기 변형을 함께 고려해야 한다."),
    _candidate("NCS-EVAL-023", "직무분석 방법에 대한 지식", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "knowledge", "natural_language"), challenge="직무분석 절차·도구·방법론이 여러 KSA로 나뉠 수 있다."),
    _candidate("NCS-EVAL-024", "공정하고 객관적인 태도", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "attitude", "natural_language"), challenge="범용 태도 표현이라 HR 범위 밖 결과가 섞일 수 있다."),
    _candidate("NCS-EVAL-025", "개인정보 보호 준수 태도", group="direct_ksa", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa", "attitude", "multi_token"), challenge="보안·법규·윤리 KSA 사이의 의미 경계를 검토해야 한다."),
    _candidate("NCS-EVAL-026", "데이터 분석가", group="cross_domain_natural", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("natural_language", "multi_token", "cross_domain"), challenge="직업명과 NCS 능력단위 명칭이 직접 일치하지 않을 수 있다."),
    _candidate("NCS-EVAL-027", "품질관리 담당자 교육", group="cross_domain_natural", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("natural_language", "multi_token", "cross_domain"), challenge="직무·역할·교육 의도가 한 질의에 결합돼 있다."),
    _candidate("NCS-EVAL-028", "생산관리 직무 역량", group="cross_domain_natural", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("natural_language", "multi_token", "cross_domain"), challenge="상위 직무명과 역량 증거가 서로 다른 결과 유형에 존재한다."),
    _candidate("NCS-EVAL-029", "고객 상담 교육", group="cross_domain_natural", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("natural_language", "multi_token", "cross_domain"), challenge="상담 도메인이 넓어 교육 의도에 맞는 범위 확인이 필요하다."),
    _candidate("NCS-EVAL-030", "안전관리자 훈련", group="cross_domain_natural", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("natural_language", "multi_token", "cross_domain"), challenge="자격·직무·훈련 표현을 검색 결과만으로 확정하면 안 된다."),
    _candidate("NCS-EVAL-031", "면접", group="short_term", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable"), challenge="두 음절 단독 질의의 후보 확장과 정밀도를 함께 관찰한다."),
    _candidate("NCS-EVAL-032", "평가", group="short_term", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable", "broad_term"), challenge="다수 분야에 공통으로 등장하는 매우 넓은 두 음절 질의다."),
    _candidate("NCS-EVAL-033", "임금", group="short_term", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable"), challenge="짧은 HR 법·제도 용어가 결과 유형별로 노출되는지 본다."),
    _candidate("NCS-EVAL-034", "노무", group="short_term", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable"), challenge="복합어 내부의 두 음절 부분 문자열 검색 동작을 확인한다."),
    _candidate("NCS-EVAL-035", "보상", group="short_term", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "short_korean", "two_syllable", "broad_term"), challenge="HR 보상과 비HR 의미가 함께 존재할 수 있어 사람 검토가 필요하다."),
    _candidate("NCS-EVAL-036", "채용/면접", group="punctuation_variant", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "punctuation", "slash", "variant_pair"), challenge="슬래시 정규화 후 원문 SQL 매칭의 비대칭을 드러낼 수 있다."),
    _candidate("NCS-EVAL-037", "채용-면접", group="punctuation_variant", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "punctuation", "hyphen", "variant_pair"), challenge="하이픈과 공백 표기 간 후보 집합 차이를 관찰한다."),
    _candidate("NCS-EVAL-038", "인사·노무", group="punctuation_variant", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "punctuation", "middle_dot"), challenge="가운뎃점으로 연결된 인접 도메인 용어의 정규화가 필요하다."),
    _candidate("NCS-EVAL-039", "평가,보상", group="punctuation_variant", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "punctuation", "comma"), challenge="쉼표 제거 후 AND·OR 완화 단계의 결과 변화를 관찰한다."),
    _candidate("NCS-EVAL-040", "KSA/교육", group="punctuation_variant", intent="evidence_analysis", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "punctuation", "slash", "latin_token"), challenge="영문 약어와 한글 용어가 혼합된 구두점 질의다."),
    _candidate("NCS-EVAL-041", "  신입사원   채용   면접  ", group="whitespace_variant", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "whitespace", "repeated_space", "variant_pair"), challenge="선행·후행 및 반복 공백 정규화의 재현성을 확인한다."),
    _candidate("NCS-EVAL-042", "데이터    분석가", group="whitespace_variant", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("whitespace", "repeated_space", "variant_pair", "cross_domain"), challenge="반복 공백이 기본 자연어 질의와 같은 후보를 내는지 관찰한다."),
    _candidate("NCS-EVAL-043", "품질관리\t담당자 교육", group="whitespace_variant", intent="task_training", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("whitespace", "tab", "variant_pair", "cross_domain"), challenge="탭 문자가 토큰 경계로 안전하게 정규화되는지 확인한다."),
    _candidate("NCS-EVAL-044", "교육 훈 련", group="whitespace_variant", intent="education_system_design", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("hr_core", "whitespace", "split_token"), challenge="한 단어가 비정상적으로 분절된 경우 완화 검색의 한계를 본다."),
    _candidate("NCS-EVAL-045", "근로 기준법", group="whitespace_variant", intent="evidence_analysis", scope="ksa", preferred_types=("ksa",), tags=("hr_core", "direct_ksa_variant", "whitespace", "split_token"), challenge="합성 법률명 내부 공백이 직접 KSA 검색에 미치는 영향을 본다."),
    _candidate("NCS-EVAL-046", "양자컴퓨팅 큐비트 오류보정", group="sparse_unrelated", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("sparse", "unrelated", "negative_control", "off_scope_candidate"), challenge="NCS 내 희소 신기술 표현에서 무리한 OR 완화가 잡음을 만들 수 있다."),
    _candidate("NCS-EVAL-047", "해저 화산 탐사 잠수정", group="sparse_unrelated", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("sparse", "unrelated", "negative_control", "off_scope_candidate"), challenge="부분 토큰만으로 넓은 결과가 생성되는지 보는 음성 대조 후보다."),
    _candidate("NCS-EVAL-048", "중세 라틴어 필사본 복원", group="sparse_unrelated", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("sparse", "unrelated", "negative_control", "off_scope_candidate"), challenge="희소 문화유산 표현에 대한 zero-hit와 잡음 후보를 관찰한다."),
    _candidate("NCS-EVAL-049", "우주 엘리베이터 케이블 설계", group="sparse_unrelated", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("sparse", "unrelated", "negative_control", "off_scope_candidate"), challenge="OR 완화 시 일반 설계 토큰만 과대 반영될 가능성이 있다."),
    _candidate("NCS-EVAL-050", "제주 감귤 디저트 브랜딩", group="sparse_unrelated", intent="structure_search", scope="all", preferred_types=("unit", "element", "criteria", "ksa"), tags=("sparse", "unrelated", "negative_control", "off_scope_candidate"), challenge="부분적으로 존재할 법한 토큰과 전체 의도 적합성을 분리해 봐야 한다."),
)


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    return {
        "min": round(min(values), 3),
        "p50": round(statistics.median(values), 3),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3),
    }


def _result_label(item: dict[str, Any]) -> str:
    for key in (
        "name",
        "title",
        "unit_name",
        "element_name",
        "criteria_text",
        "ksa_text",
        "text",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    return ""


def summarize_catalog(candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cases = list(candidates)
    tags = Counter(tag for case in cases for tag in case["tags"])
    groups = Counter(case["domain_group"] for case in cases)
    intents = Counter(case["intent_candidate"] for case in cases)
    scopes = Counter(case["scope_candidate"] for case in cases)
    return {
        "candidate_count": len(cases),
        "unique_query_count": len({case["query"] for case in cases}),
        "group_counts": dict(sorted(groups.items())),
        "tag_counts": dict(sorted(tags.items())),
        "intent_candidate_counts": dict(sorted(intents.items())),
        "scope_candidate_counts": dict(sorted(scopes.items())),
        "minimum_coverage_checks": {
            "exactly_50_candidates": len(cases) == 50,
            "hr_core_at_least_15": tags["hr_core"] >= 15,
            "direct_ksa_at_least_10": tags["direct_ksa"] >= 10,
            "two_syllable_present": tags["two_syllable"] > 0,
            "punctuation_present": tags["punctuation"] > 0,
            "whitespace_present": tags["whitespace"] > 0,
            "sparse_unrelated_present": tags["sparse"] > 0 and tags["unrelated"] > 0,
        },
    }


def load_runtime_search(db_path: Path) -> SearchFunction:
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp.server import search_ncs

    return search_ncs


def measure_candidates(
    candidates: Iterable[dict[str, Any]],
    search_fn: SearchFunction,
    *,
    runs: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    all_latencies: list[float] = []
    type_case_presence: Counter[str] = Counter()
    for case in candidates:
        elapsed: list[float] = []
        count_samples: list[int] = []
        last_payload: dict[str, Any] = {}
        last_results: list[dict[str, Any]] = []
        for _ in range(runs):
            started = time.perf_counter()
            payload = search_fn(case["query"], case["scope_candidate"], limit)
            duration = (time.perf_counter() - started) * 1000
            elapsed.append(duration)
            all_latencies.append(duration)
            last_payload = payload if isinstance(payload, dict) else {}
            raw_results = last_payload.get("results")
            last_results = raw_results if isinstance(raw_results, list) else []
            count_samples.append(len(last_results))
        raw_counts = last_payload.get("counts_by_type")
        if isinstance(raw_counts, dict):
            counts_by_type = {
                str(key): int(value)
                for key, value in raw_counts.items()
                if isinstance(value, int)
            }
        else:
            counts_by_type = dict(
                Counter(str(item.get("type") or "unknown") for item in last_results)
            )
        for result_type, count in counts_by_type.items():
            if count > 0:
                type_case_presence[result_type] += 1
        preferred = case["preferred_result_type_candidates"]
        preferred_present = [name for name in preferred if counts_by_type.get(name, 0) > 0]
        records.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "measurement_scope": case["scope_candidate"],
                "result_count": count_samples[-1],
                "result_count_samples": count_samples,
                "result_count_stable": len(set(count_samples)) <= 1,
                "zero_hit_observed": count_samples[-1] == 0,
                "counts_by_type": dict(sorted(counts_by_type.items())),
                "preferred_result_type_candidates_present": preferred_present,
                "preferred_type_candidate_coverage": round(
                    len(preferred_present) / len(preferred), 4
                )
                if preferred
                else None,
                "latency_ms": _latency_summary(elapsed),
                "preview": [
                    {
                        "type": item.get("type"),
                        "id": item.get("id"),
                        "label": _result_label(item),
                    }
                    for item in last_results[:5]
                    if isinstance(item, dict)
                ],
                "relevance_judgment_present": False,
            }
        )
    zero_hits = [record["case_id"] for record in records if record["zero_hit_observed"]]
    coverage_values = [
        record["preferred_type_candidate_coverage"]
        for record in records
        if isinstance(record["preferred_type_candidate_coverage"], float)
    ]
    aggregate = {
        "measured_case_count": len(records),
        "zero_hit_count": len(zero_hits),
        "zero_hit_rate": round(len(zero_hits) / len(records), 4) if records else None,
        "zero_hit_case_ids": zero_hits,
        "latency_ms_across_calls": _latency_summary(all_latencies),
        "case_presence_by_result_type": dict(sorted(type_case_presence.items())),
        "mean_preferred_type_candidate_coverage": round(
            statistics.mean(coverage_values), 4
        )
        if coverage_values
        else None,
        "stable_result_counts": all(record["result_count_stable"] for record in records),
        "interpretation": (
            "Observed candidate retrieval only. Type presence is not a relevance label and does "
            "not establish Recall@K, MRR, MAP, or NDCG."
        ),
    }
    return records, aggregate


def build_report(
    *,
    db_path: Path,
    runs: int,
    limit: int,
    measure: bool,
    search_fn: SearchFunction | None = None,
    candidates: Iterable[dict[str, Any]] = CANDIDATES,
) -> dict[str, Any]:
    cases = [dict(case) for case in candidates]
    measurement: dict[str, Any]
    if measure:
        runtime_search = search_fn or load_runtime_search(db_path)
        records, aggregate = measure_candidates(
            cases, runtime_search, runs=runs, limit=limit
        )
        measurement = {
            "executed": True,
            "search_contract": "ncs_mcp.server.search_ncs",
            "scope_policy": "measure each candidate using its unreviewed scope_candidate",
            "runs_per_case": runs,
            "limit": limit,
            "records": records,
            "aggregate": aggregate,
        }
    else:
        measurement = {
            "executed": False,
            "reason": "measurement_skipped",
            "records": [],
            "aggregate": {},
        }
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": _generated_at(),
        "mode": "read_only_candidate_evaluation",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database_path": str(db_path.resolve()),
            "latency_caveat": (
                "Measured latency is local and warm-biased; it is not a Vercel cold-start value."
            ),
        },
        "evaluation_contract": {
            "status": "candidate_eval",
            "gold_dataset": False,
            "gold_labels_present": False,
            "automatic_relevance_labels": False,
            "human_review_required": True,
            "human_decision_present": False,
            "approval_claim": False,
            "automatic_proxy_metrics": ["zero_hit", "type_presence", "latency_ms"],
            "metrics_requiring_human_labels": [
                "Recall@5",
                "Recall@10",
                "MRR@10",
                "nDCG@10",
                "off_scope_rate",
            ],
        },
        "catalog_summary": summarize_catalog(cases),
        "candidates": cases,
        "current_search_observation": measurement,
        "commands": {
            "reproduce": (
                "python scripts/build_ncs_search_eval_pack.py "
                f"--db \"{db_path.resolve()}\" --runs {runs} --limit {limit}"
            ),
            "candidate_only": "python scripts/build_ncs_search_eval_pack.py --skip-measure",
        },
        "safety": {
            "database_open_mode": "read_only",
            "database_writes": False,
            "raw_ksa_mutation": False,
            "status_updates": False,
            "human_approval_written": False,
        },
    }


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\t", "<TAB>").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["catalog_summary"]
    contract = report["evaluation_contract"]
    observation = report["current_search_observation"]
    records = {
        record["case_id"]: record for record in observation.get("records", [])
    }
    lines = [
        "# NCS Search Evaluation Candidate Pack",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Schema: `{report['schema']}`",
        f"- Status: `{contract['status']}`",
        f"- Candidate count: `{summary['candidate_count']}`",
        "- Gold dataset: `false`",
        "- Human review required: `true`",
        "- These queries and preferred result types are candidates, not relevance judgments.",
        "",
        "## Coverage",
        "",
    ]
    for name, count in summary["group_counts"].items():
        lines.append(f"- Group `{name}`: `{count}`")
    for name, passed in summary["minimum_coverage_checks"].items():
        lines.append(f"- Check `{name}`: `{str(passed).lower()}`")
    if observation.get("executed"):
        aggregate = observation["aggregate"]
        lines.extend(
            [
                "",
                "## Current Search Observation",
                "",
                f"- Measured cases: `{aggregate['measured_case_count']}`",
                f"- Zero-hit count: `{aggregate['zero_hit_count']}`",
                f"- Zero-hit rate: `{aggregate['zero_hit_rate']}`",
                f"- Call p50 ms: `{aggregate['latency_ms_across_calls']['p50']}`",
                f"- Call p95 ms: `{aggregate['latency_ms_across_calls']['p95']}`",
                f"- Stable result counts: `{aggregate['stable_result_counts']}`",
                f"- {aggregate['interpretation']}",
            ]
        )
    lines.extend(
        [
            "",
            "## Candidate Queries",
            "",
            "| ID | Query | Group | Intent candidate | Scope candidate | Tags | Observed count | Observed types | p50 ms |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- | ---: |",
        ]
    )
    for case in report["candidates"]:
        record = records.get(case["case_id"], {})
        counts = record.get("counts_by_type", {})
        types = ", ".join(f"{key}:{value}" for key, value in counts.items()) or "not measured"
        p50 = record.get("latency_ms", {}).get("p50", "n/a")
        values = (
            case["case_id"],
            case["query"],
            case["domain_group"],
            case["intent_candidate"],
            case["scope_candidate"],
            ", ".join(case["tags"]),
            record.get("result_count", "n/a"),
            types,
            p50,
        )
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "## Interpretation and Safety",
            "",
            "- Zero-hit and type coverage are retrieval observations, not proof of relevance.",
            "- Recall@K, MRR, MAP, and NDCG require explicit human relevance judgments.",
            "- No candidate is marked as gold, accepted, reviewed, or approved.",
            "- The database is opened through the read-only runtime contract; no DB writes occur.",
            "- `ksa_items.ksa_text_raw` is not modified.",
            "",
            "## Reproduce",
            "",
            "```powershell",
            report["commands"]["reproduce"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a human-gated 50-query candidate evaluation pack for NCS search."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--skip-measure", action="store_true")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.runs > 10:
        parser.error("--runs must be between 1 and 10")
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    if not args.skip_measure and not args.db.is_file():
        parser.error(f"database not found: {args.db}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        db_path=args.db,
        runs=args.runs,
        limit=args.limit,
        measure=not args.skip_measure,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "candidate_count": report["catalog_summary"]["candidate_count"],
                "coverage_checks": report["catalog_summary"]["minimum_coverage_checks"],
                "observation": report["current_search_observation"].get("aggregate", {}),
                "gold_dataset": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
