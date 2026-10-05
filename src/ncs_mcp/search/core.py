from __future__ import annotations

import bisect
import hashlib
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from typing import Any

from ncs_mcp.query_router import (
    NCS_SEARCH_CONTEXT_RESOLVER_VERSION,
    NCS_SEARCH_CONTEXT_SCHEMA,
    _strip_ncs_standard_request_prefix,
    normalize_search_context_inputs,
    search_context_request_contract,
)

from .prefix_index import (
    PREFIX_FTS_REQUIRED_MANIFEST,
    PREFIX_FTS_TABLES,
    prefix_fts_term,
)
from .semantic_rescue import (
    DEFAULT_RESCUE_MARGIN,
    SemanticSimilarityProvider,
    rescue_order,
)
from .normalization import (
    SEARCH_NORMALIZATION_FIELDS,
    SEARCH_NORMALIZATION_REQUIRED_MANIFEST,
    SEARCH_NORMALIZATION_SOURCE_FIELDS,
    SEARCH_NORMALIZATION_V2_FIELDS,
    SEARCH_NORMALIZATION_V2_OVERRIDES,
    SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST,
    normalize_search_text,
)


_OPEN_DB_FACTORY: Any = None
_CLAMP_LIMIT: Any = None
_UNIT_PATH: Any = None
_TIER_PREDICATES: Any = None
_TIER_EXECUTOR: Any = None
_TOKEN_EXPANDER: Any = None
_SEMANTIC_PROVIDER: SemanticSimilarityProvider | None = None
_SEMANTIC_MARGIN: float = DEFAULT_RESCUE_MARGIN

_NCS_CLASSIFICATION_FILTER_FIELDS = (
    "major_code",
    "middle_code",
    "small_code",
    "sub_code",
    "major_name",
    "middle_name",
    "small_name",
    "sub_name",
)
_NCS_IGNORED_FILTER_KEY_PREVIEW_LIMIT = 8
_NCS_IGNORED_FILTER_KEY_MAX_LENGTH = 64


def configure_search_runtime(
    *,
    open_db_factory: Any,
    clamp_limit: Any,
    unit_path: Any,
    tier_predicates: Any = None,
    tier_executor: Any = None,
    token_expander: Any = None,
    semantic_provider: SemanticSimilarityProvider | None = None,
    semantic_margin: float = DEFAULT_RESCUE_MARGIN,
) -> None:
    """Inject server-owned runtime helpers without importing the server module.

    ``semantic_provider`` is optional. Without one the unit ranking is byte
    identical to the lexical result, so no deployment gains a semantic step
    until a provider is configured on purpose.
    """
    global _OPEN_DB_FACTORY, _CLAMP_LIMIT, _UNIT_PATH
    global _TIER_PREDICATES, _TIER_EXECUTOR, _TOKEN_EXPANDER
    global _SEMANTIC_PROVIDER, _SEMANTIC_MARGIN
    _OPEN_DB_FACTORY = open_db_factory
    _CLAMP_LIMIT = clamp_limit
    _UNIT_PATH = unit_path
    _TIER_PREDICATES = tier_predicates
    _TIER_EXECUTOR = tier_executor
    _TOKEN_EXPANDER = token_expander
    _SEMANTIC_PROVIDER = semantic_provider
    _SEMANTIC_MARGIN = semantic_margin


def _required_runtime_helper(name: str, helper: Any) -> Any:
    if helper is None:
        raise RuntimeError(f"NCS search runtime helper is not configured: {name}")
    return helper


def _active_tier_predicates() -> Any:
    return _TIER_PREDICATES or _ncs_search_tier_predicates


def _active_tier_executor() -> Any:
    return _TIER_EXECUTOR or _execute_ncs_search_tiers


def _active_token_expander() -> Any:
    return _TOKEN_EXPANDER or _validated_ncs_search_token_expansions


def _ncs_search_markdown(
    query: str,
    results: list[dict[str, Any]],
    *,
    counts_by_type: dict[str, int],
    offset: int,
    next_offset: int | None,
) -> str:
    lines = [f"## NCS 검색 결과: {query}"]
    lines.append(f"- 반환 {len(results)}건 중 최대 5건 미리보기")
    type_summary = ", ".join(
        f"{item_type} {count}건"
        for item_type, count in counts_by_type.items()
        if count > 0
    )
    if type_summary:
        lines.append(f"- 유형별 반환: {type_summary}")
    lines.append(f"- 현재 페이지: `offset={offset}`")
    if next_offset is not None:
        lines.append(
            f"- 다음 페이지: 같은 질의와 범위에 `offset={next_offset}`을 지정하세요."
        )
    for index, item in enumerate(results[:5], start=1):
        item_type = str(item.get("type") or "result")
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        lines.append(f"{index}. **{text}** (`{item_type}` · `{item_id}`)")
    return "\n".join(lines)


def _ncs_search_leaf_path(row: Any, *, include_element: bool = False) -> dict[str, Any]:
    """Expose a leaf result's source classification without another query."""
    path = _required_runtime_helper("unit_path", _UNIT_PATH)(row)
    path.update({"unit_code": row["unit_code"], "unit_name": row["unit_name_raw"]})
    if include_element:
        path.update(
            {
                "element_id": row["element_id"],
                "element_name": row["element_name_raw"],
            }
        )
    return path


_NCS_SEARCH_TYPES = ("unit", "element", "criteria", "ksa")
_NCS_SEARCH_MATCH_MODES = {
    -1: "intent_alias",
    0: "phrase",
    1: "token_and",
    2: "expanded_token_and",
    3: "token_or",
    4: "morphology_fill",
}
_NCS_SEARCH_LOW_INFORMATION_SUFFIXES = (
    "관리",
    "운영",
    "업무",
    "직무",
    "실무",
)
# These terms are either frequent workflow nouns/verbs in NCS definitions or
# actor/context words that carry little domain-specific intent by themselves.
# They still contribute to fallback ranking, but cannot be the sole fallback hit.
_NCS_SEARCH_GENERIC_TOKENS = frozenset(
    {
        "관리",
        "제도",
        "설계",
        "계획",
        "수립",
        "운영",
        "업무",
        "직원",
        "담당",
        # High-frequency workflow verbs/nouns that appear across many majors.
        # Kept out of sole token-OR hits so rare definition evidence (for
        # example 법인카드 inside 자금관리) is not buried under name matches
        # such as 사용승인 관리 or 기본공구 사용.
        "사용",
        "제작",
        "작성",
        "방지",
        "발행",
        "수취",
        "안내",
        "진행",
        "구성",
        "마련",
        "점검",
        "예방",
        "조직",
        "발표",
        "자료",
        "내역",
        "서류",
    }
)
_NCS_SEARCH_GENERIC_TOKEN_FACTOR = 0.3
# Bare token-OR soft prior keeps coverage as a tie-break only. Public major
# diversity is not applied: same-major true positives must keep lexical order.
_NCS_SEARCH_MAJOR_DIVERSITY_WINDOW = 5
_NCS_SEARCH_MAJOR_DIVERSITY_MAX_PER_MAJOR = 2
# Fallback scoring weighs each token by how few unit names contain it.  A hand
# kept generic list only covers the words someone thought of: 퇴직 names 2 units
# and 처리 names 195, but both scored 1.0, so a lone 처리 hit tied with a lone
# 퇴직 hit and the shorter name won the length tiebreak -- which is how
# 퇴직 정산 서류 처리 returned 심냉처리 and 퀜칭열처리.  Document frequency is
# measured over unit names, the highest weighted field, and normalized to
# (0, 1] so score magnitudes stay in the range the tiers already assume.
_NCS_SEARCH_IDF_FLOOR = 0.05
# Definitions describe the work performed by a unit.  Give them enough weight
# to beat a name-only candidate when the query contains concrete task terms,
# while keeping the unit name as the strongest single field.
_NCS_SEARCH_DEFINITION_WEIGHT = 2.0
# Task/KSA evidence is a supporting signal for the weakest lexical fallback.
# It is deliberately below the unit-name/definition weights so that broad
# evidence cannot override an exact or token-AND match.
_NCS_SEARCH_TASK_KSA_WEIGHT = 0.5
# Rerank a fixed lexical prefix before slicing pages. Using the requested page
# size here makes limit=3 discard evidence that limit=50 would rank first.
_NCS_SEARCH_UNIT_RERANK_WINDOW = 50
# Leaf (element, criterion, KSA) fallback predicates use at most this many raw
# query tokens.  Unit search resolves longer queries itself; see
# _select_ncs_search_unit_terms.
_NCS_SEARCH_FALLBACK_TOKEN_BOUND = 4
# Public-search recall equivalences bridge practitioner language to official NCS
# names.  They are candidate-only expansions, not source evidence or DB writes.
_NCS_SEARCH_QUERY_EQUIVALENTS = {
    "성과평가": ("인사평가",),
}
# High-specificity practitioner phrases whose official NCS unit terminology is
# materially different.  Keep these as candidate-only retrieval hints: they do
# not alter source data, review status, or ontology evidence.
_NCS_SEARCH_QUERY_INTENT_EQUIVALENTS = {
    "연봉 협상": ("임금관리",),
    "퇴직금 정산": ("퇴직업무지원", "급여지급"),
    "온보딩": ("인력채용", "교육훈련운영"),
    "승진 심사": ("인력이동관리",),
    "직원 고충": ("노사갈등 해결",),
    "노사관계 성과 평가": ("노사관계 평가",),
    "노사 교육": ("노사관계 교육훈련",),
    "사내 행사": ("행사지원관리",),
    "사무용품": ("비품관리",),
    "법인 차량": ("차량운영관리",),
    "법인카드": ("자금관리",),
    "사내 복지": ("복리후생지원",),
    "사옥 보안": ("총무보안관리",),
    "임직원 보안": ("총무보안관리",),
    "사내 보안": ("총무보안관리",),
    "용역 계약": ("용역관리",),
    "시설관리 용역": ("용역관리",),
    "프레젠테이션 자료": ("사무자동화 프로그램 활용", "문서 작성"),
    "발표 자료": ("사무자동화 프로그램 활용", "문서 작성"),
    "사내강사": ("교수활동 수행",),
    "교안 작성": ("교수활동 수행",),
    "강의안": ("교수활동 수행",),
    "평가문항": ("교육과정 개발",),
    "교육 성과 지표": ("교육운영기획",),
    "평가지표 운용": ("교육운영기획",),
    "LMS": ("교육자원관리",),
    "학습관리시스템": ("교육자원관리",),
    "학습조직": ("학습조직구축",),
    "사내 학습동아리": ("학습조직구축",),
    "기업 학습동아리": ("학습조직구축",),
    "학습 동아리": ("학습조직구축",),
    "학습동아리": ("학습조직구축",),
    "4대보험": ("급여지급",),
    "연말정산": ("원천징수", "급여지급"),
    "인건비 예산": ("인사기획",),
    "노동관계법": ("노사갈등 해결",),
    "노사 분쟁": ("노사갈등 해결",),
    "교섭 위원": ("단체교섭준비",),
    "교섭안": ("단체교섭준비",),
    "법인 인감": ("업무지원",),
    "인감 날인": ("업무지원",),
    "부서 일정": ("사무행정 업무 관리",),
    "사무행정": ("사무행정 업무 관리",),
    "연결재무제표": ("사업결합회계",),
    "비영리 회계": ("비영리회계",),
    "비영리법인": ("비영리회계",),
    "교육과정 콘텐츠": ("교육과정 개발",),
    "교육 프로그램 콘텐츠": ("교육과정 개발",),
    "인력 수급": ("인사기획",),
    "교육 수요": ("교육체계 수립",),
    "문서 보관": ("총무문서관리",),
    "문서 폐기": ("총무문서관리",),
    "출장 증명": ("업무지원",),
    "증명서 발급": ("업무지원",),
    "근태": ("급여지급",),
    "임금피크": ("임금관리",),
    "교육 참여율": ("교육성과 평가",),
    "만족도 집계": ("교육성과 평가",),
    "강사 섭외": ("교육자원관리",),
    "사내 교육 강사": ("교육자원관리",),
    "사무실 이전": ("업무지원",),
    "연간 행사": ("행사지원관리",),
    "자금 수지": ("자금관리",),
    "인사전략": ("인사기획",),
    "직무 등급": ("직무관리",),
    "배치전환": ("인력이동관리",),
    "협약 체결": ("단체교섭",),
    "취업규칙": ("단체협약이행",),
    "자료 보안": ("자료 관리",),
    "손익분기점": ("원가관리",),
    "CVP 분석": ("원가관리",),
    "재무제표 작성": ("재무제표작성",),
    "원천세": ("원천징수",),
    "부가세": ("부가가치세 신고",),
    "세금계산서": ("부가가치세 신고",),
    # Cross-domain practitioner phrases: role/process words beat generic
    # "고객 불만" / "자동차" / "얼굴" lexical traps that bury the official unit.
    "경비원": ("경비고객관계관리",),
    "사고 현장 조사": ("차량사고 현장조사",),
    "사고 현장조사": ("차량사고 현장조사",),
    "교육과정 설계": ("교육과정 설계",),
    "메이크업": ("베이스 메이크업",),
}
_NCS_SEARCH_QUERY_INTENT_BLOCKERS = {
    "연봉 협상": ("선수", "스포츠", "프로야구", "프로축구", "구단"),
    # Keep clinical/hospital payroll in its source major; do not rewrite to
    # the general HR 급여지급 unit via the 4대보험 practitioner hint.
    "4대보험": ("병원", "의료", "간호", "환자", "클리닉", "의사"),
    # Manufacturing/ops staffing plans should not collapse into HR 인사기획.
    "인력 수급": ("조업", "생산", "제조", "공정", "설비"),
    # Social-welfare training surveys are not corporate 교육체계 수립.
    "교육 수요": ("사회복지", "복지관", "자원봉사", "청소년"),
    # Chemical/regulatory filings must not collapse into labor 취업규칙.
    "취업규칙": ("화학", "허가", "환경", "산업안전"),
}


def _normalize_ncs_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"[A-Za-z0-9]+_[A-Za-z0-9]+", text):
        return text
    normalized = [
        " " if character.isspace() or unicodedata.category(character).startswith("P") else character
        for character in text
    ]
    return re.sub(r"\s+", " ", "".join(normalized)).strip()


_NCS_SEARCH_REQUEST_VERB = (
    r"(?:찾아|알려|보여|검색해|조회해|설명해|정리해)\s*(?:줘요?|주세요|주십시오)"
)
_NCS_SEARCH_EVIDENCE_NOUN = (
    r"(?:수행\s*준거|능력\s*단위\s*요소|능력\s*단위|지식|기술|태도|KSA)"
    r"(?:\s*\([KSA]\))?"
)
_NCS_SEARCH_REQUEST_FRONT = re.compile(
    rf"(?:다음\s+)?(?:직무|업무|과업|능력단위)(?:를|을)?\s*"
    rf"{_NCS_SEARCH_REQUEST_VERB}\s*[:：]\s*(?P<subject>.+)", re.IGNORECASE,
)
_NCS_SEARCH_REQUEST_END = re.compile(rf"{_NCS_SEARCH_REQUEST_VERB}$", re.IGNORECASE)
_NCS_SEARCH_REQUEST_TAILS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"(?P<subject>.+?)(?:에\s*(?:대한|필요한)|에서\s*필요한|의|\s+관련)\s*"
    rf"{_NCS_SEARCH_EVIDENCE_NOUN}"
    rf"(?:(?:\s*(?:과|와|및|,|·|/)\s*|\s+){_NCS_SEARCH_EVIDENCE_NOUN})*"
    rf"(?:를|을)?\s*{_NCS_SEARCH_REQUEST_VERB}",
    rf"(?P<subject>.+?)에\s*(?:대해(?:서)?|관해(?:서)?)\s*{_NCS_SEARCH_REQUEST_VERB}",
    rf"(?P<subject>.+?)\s+{_NCS_SEARCH_REQUEST_VERB}",
))


def _ncs_search_prompt_subject(query: str) -> str:
    """Remove explicit request framing, never arbitrary domain stopwords.

    A full conversational prefix must not consume the four-token fallback
    budget before the actual subject. Only complete, anchored requests are
    recognized. Keep all subject qualifiers, alternatives and negations;
    classification filters still apply independently. No domain aliases or
    relevance labels are introduced. The public ``query`` retains the input,
    and ``normalized_query`` exposes the effective search subject.
    """
    original = unicodedata.normalize("NFKC", str(query or "")).strip()
    text = re.sub(r"\s+", " ", original).rstrip(".!?。！？ ")
    without_intro = _strip_ncs_standard_request_prefix(text)
    front = _NCS_SEARCH_REQUEST_FRONT.fullmatch(without_intro)
    subject = front.group("subject") if front else None
    if subject is None:
        if not _NCS_SEARCH_REQUEST_END.search(text):
            return original
        for pattern in _NCS_SEARCH_REQUEST_TAILS:
            match = pattern.fullmatch(text)
            if match:
                subject = match.group("subject")
                break
    if subject is None:
        return original
    subject = _strip_ncs_standard_request_prefix(subject)
    subject = re.sub(
        r"^우리\s+(?:회사|조직)에서\s*수행하는\s*(?:업무|직무|과업)\s*중\s+",
        "", subject, count=1,
    ).strip()
    if len(subject) >= 2 and (subject[0], subject[-1]) in (
        ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    ):
        subject = subject[1:-1].strip()
    # Incomplete requests contain no independently specified search subject.
    if not subject or re.fullmatch(
        rf"(?:{_NCS_SEARCH_EVIDENCE_NOUN}|직무|업무|과업)(?:를|을)?", subject, re.IGNORECASE,
    ):
        return original
    return subject


def _normalize_ncs_search_query(query: str) -> tuple[str, list[str], list[str]]:
    normalized = _normalize_ncs_search_text(_ncs_search_prompt_subject(query))
    query_tokens = normalized.split()[:_NCS_SEARCH_FALLBACK_TOKEN_BOUND]
    fallback_tokens = [token for token in query_tokens if len(token) > 1]
    # Bound the expensive fallback predicates, not the literal phrase. Official
    # unit names can exceed four tokens and differ only in their final words.
    return normalized, query_tokens, fallback_tokens


def _ncs_search_joined_compound_phrase(
    phrase: str,
    fallback_tokens: list[str],
) -> str:
    """Join a bounded all-Hangul phrase for official compound-name recall.

    NCS unit names commonly omit spaces that users naturally insert. Only join
    the complete two-to-four-token query, require every token to carry at least
    two Hangul syllables, and keep the result short. This is a spelling variant
    of the supplied phrase, not an alias or a source-data rewrite.
    """
    phrase_tokens = phrase.split()
    if not 2 <= len(phrase_tokens) <= 4:
        return ""
    if phrase_tokens != fallback_tokens:
        return ""
    if not all(re.fullmatch(r"[가-힣]{2,}", token) for token in phrase_tokens):
        return ""
    joined = "".join(phrase_tokens)
    return joined if len(joined) <= 24 else ""


def _ncs_search_joined_compound_subphrases(
    fallback_tokens: list[str],
) -> dict[str, list[str]]:
    """Build bounded two-token compound candidates for unit-name recall.

    Users often insert spaces inside an official NCS unit name (for example
    ``경영 정보 대시보드 시각화`` for ``경영정보시각화``).  The full-query
    join handled by :func:`_ncs_search_joined_compound_phrase` cannot recover
    that case when an extra descriptive token is present.  This helper adds
    only adjacent two-token joins, and only as unit-name candidates.  It is
    deliberately not an alias and is never applied to element, criteria, or
    KSA text where a short compound can be a homograph.

    Existing particle stripping is reused solely to form the candidate term;
    the original query token remains the group key and is still reported in
    search metadata.  Lexical-boundary SQL checks reject an internal match,
    so ``출입 계약`` cannot match the ``출입`` substring inside
    ``수출입계약``.
    """
    if len(fallback_tokens) < 3:
        return {}
    if not all(re.fullmatch(r"[가-힣]{2,}", token) for token in fallback_tokens):
        return {}
    morphology = _ncs_search_morphology_expansions(fallback_tokens)
    stems = [morphology.get(token, [token])[0] for token in fallback_tokens]
    if not all(re.fullmatch(r"[가-힣]{2,}", token) for token in stems):
        return {}

    expansions: dict[str, list[str]] = {}
    for index in range(len(stems) - 1):
        joined = "".join(stems[index:index + 2])
        if len(joined) > 24:
            continue
        for token in fallback_tokens[index:index + 2]:
            values = expansions.setdefault(token, [])
            if joined not in values and joined != token:
                values.append(joined)
    return expansions


def _ncs_search_morphology_expansions(tokens: list[str]) -> dict[str, list[str]]:
    """Offer one conservative Korean particle removal per bounded query token.

    These are retrieval candidates, not a tokenizer or aliases. Preserve the
    original query and only use stems after every original lexical tier. Paired
    particles must agree with the final Hangul consonant; never peel repeatedly
    or reduce a token to a single syllable.
    """
    particles = (
        ("으로", "consonant_except_rieul"), ("에서", "any"),
        ("에게", "any"), ("부터", "any"), ("까지", "any"),
        ("은", "consonant"), ("는", "vowel"),
        ("을", "consonant"), ("를", "vowel"),
        ("이", "consonant"), ("가", "vowel"),
        ("과", "consonant"), ("와", "vowel"),
        ("로", "vowel_or_rieul"), ("의", "any"), ("에", "any"),
    )
    expansions: dict[str, list[str]] = {}
    for token in tokens[:4]:
        if not re.fullmatch(r"[가-힣]{3,}", token):
            continue
        for suffix, rule in particles:
            if not token.endswith(suffix):
                continue
            stem = token[:-len(suffix)]
            if len(stem) < 2:
                continue
            final = (ord(stem[-1]) - 0xAC00) % 28
            allowed = (
                rule == "any"
                or (rule == "consonant" and final != 0)
                or (rule == "vowel" and final == 0)
                or (rule == "consonant_except_rieul" and final not in (0, 8))
                or (rule == "vowel_or_rieul" and final in (0, 8))
            )
            if allowed:
                expansions[token] = [stem]
            break
    return expansions


# Long practitioner sentences ("올해 정원 대비 현원을 분석해서 ... 세우려고
# 합니다") used to reach unit ranking as their first four raw words, particles
# and all, so the subject words at the end never matched and words such as 대비
# pulled 비상상황 대비 to first place.  Unit search now resolves every word
# against the unit corpus, then keeps up to _NCS_SEARCH_UNIT_TERM_LIMIT terms.
# These lists only describe query wording; they never touch source text,
# evidence, or review state.
_NCS_SEARCH_UNIT_TERM_LIMIT = 6
_NCS_SEARCH_LONG_QUERY_STOPWORDS = frozenset(
    {
        # Time and frequency framing.
        "올해", "금년", "작년", "내년", "내년도", "상반기", "하반기",
        "매달", "매월", "매주", "매일", "매년", "분기마다",
        # Connectives and relational nouns.
        "때", "때마다", "다음", "후", "뒤", "전", "위해", "위한", "대비",
        "대해", "대한", "관련", "관련된", "같은", "및", "등", "또는",
        "그리고", "새로", "따로", "각종", "모든", "전체", "사이", "얼마나",
        "어떻게", "무엇", "하나", "데", "것", "수", "중", "안", "내",
        # Requester framing and filler nouns.  Exact official names still
        # match through the untouched phrase tier.
        "우리", "저희", "회사", "사내", "직원", "직원들", "일", "작업", "업무",
        "단계", "방안", "방법", "내용", "부분", "프로젝트", "프로그램",
        # Bare predicates.
        "있는", "없는", "하는", "하고", "합니다", "해야", "해요", "주세요",
        "싶습니다", "싶어요", "필요", "필요한",
    }
)
# Sino-Korean verbal nouns carry the subject; strip the predicate that follows.
_NCS_SEARCH_PREDICATE_SUFFIXES = (
    "하려고", "하려는", "합니다", "시키고", "시키는", "적으로",
    "받아서", "하는", "하고", "해서", "하여", "하기", "하며", "하면", "한다",
    "했다", "해야", "되는", "되고", "되어", "시킨", "적인", "받는", "할", "한",
    "된",
)
# Noun particles in longest-first order with the same final-consonant rules
# as _ncs_search_morphology_expansions, plus plural/distributive endings.
_NCS_SEARCH_TERM_PARTICLES = (
    ("에서는", "any"), ("으로는", "consonant_except_rieul"),
    ("마다", "any"), ("별로", "any"), ("에서", "any"), ("에게", "any"),
    ("으로", "consonant_except_rieul"), ("부터", "any"), ("까지", "any"),
    ("에는", "any"), ("에도", "any"), ("처럼", "any"), ("보다", "any"),
    ("들의", "any"), ("들이", "any"), ("들을", "any"), ("들", "any"),
    ("별", "any"), ("은", "consonant"), ("는", "vowel"),
    ("을", "consonant"), ("를", "vowel"), ("이", "consonant"),
    ("가", "vowel"), ("과", "consonant"), ("와", "vowel"),
    ("로", "vowel_or_rieul"), ("의", "any"), ("에", "any"),
)
# Endings that mark a remaining word as a predicate or modifier.  In a long
# query such a word is dropped unless an official unit name contains it.
# Syllables that also end common nouns (문서, 재고, 화면, 복지, 전기) are
# deliberately absent.
_NCS_SEARCH_PREDICATE_FINALS = (
    "는", "은", "며", "면", "던", "려고", "도록", "니다", "까요", "나요",
    "세요", "지만", "는지", "아서", "어서", "여서", "져서", "춰서", "워서",
)
_NCS_SEARCH_COMPOUND_PIECE_MIN = 2


def _ncs_search_particle_allowed(stem: str, rule: str) -> bool:
    if rule == "any":
        return True
    final = (ord(stem[-1]) - 0xAC00) % 28
    return (
        (rule == "consonant" and final != 0)
        or (rule == "vowel" and final == 0)
        or (rule == "consonant_except_rieul" and final not in (0, 8))
        or (rule == "vowel_or_rieul" and final in (0, 8))
    )


def _ncs_search_term_stems(word: str) -> list[str]:
    """Return candidate stems for one query word, most reduced first.

    A predicate suffix is removed before a noun particle so that
    ``분석해서`` yields ``분석`` and ``직원들에게`` yields ``직원``.  Stems must
    keep at least two Hangul syllables; the caller accepts a stem only when the
    unit corpus contains it, so an over-eager strip cannot invent a term.
    """
    stems: list[str] = []
    latin = re.fullmatch(r"([A-Za-z0-9]{2,})([가-힣]{1,2})", word)
    if latin:
        # DBMS를, LMS로: a Latin term followed by a bare particle.
        if any(latin.group(2) == suffix for suffix, _ in _NCS_SEARCH_TERM_PARTICLES):
            stems.append(latin.group(1))
        return stems
    if not re.fullmatch(r"[가-힣]{3,}", word):
        return stems
    current = word
    for suffix in _NCS_SEARCH_PREDICATE_SUFFIXES:
        stem = current[: -len(suffix)]
        if current.endswith(suffix) and len(stem) >= 2:
            stems.append(stem)
            current = stem
            break
    for _ in range(2):
        if len(current) < 3:
            break
        stripped = None
        for suffix, rule in _NCS_SEARCH_TERM_PARTICLES:
            stem = current[: -len(suffix)]
            if (
                current.endswith(suffix)
                and len(stem) >= 2
                and re.fullmatch(r"[가-힣]+", stem)
                and _ncs_search_particle_allowed(stem, rule)
            ):
                stripped = stem
                break
        if not stripped:
            break
        stems.append(stripped)
        current = stripped
    # Prefer the most reduced stem: 직원들에게 -> 직원 before 직원들.
    return list(dict.fromkeys(reversed(stems)))


def _ncs_search_compound_pieces(word: str) -> list[str]:
    """Return the prefix/suffix pieces of a closed compound, longest first."""
    if not re.fullmatch(r"[가-힣]{3,}", word):
        return []
    pieces: list[str] = []
    for size in range(len(word) - 1, _NCS_SEARCH_COMPOUND_PIECE_MIN - 1, -1):
        for piece in (word[-size:], word[:size]):
            if piece not in pieces:
                pieces.append(piece)
    return pieces


class _NcsUnitLexicon:
    """Word-level document frequencies of unit names and definitions.

    A term "occurs" in a unit when some word of the field starts with it,
    which is the lexical-boundary rule the search tiers use, so ``퇴직`` counts
    for ``퇴직업무지원`` but ``계도`` does not count for ``설계도``.  Counts for a
    prefix are summed over the matching words and capped at the corpus size;
    they rank specificity and never decide a match by themselves.
    """

    __slots__ = ("total", "_name_words", "_name_counts", "_any_words", "_any_counts")

    def __init__(self, rows: list[Any]) -> None:
        name_df: Counter[str] = Counter()
        any_df: Counter[str] = Counter()
        for name, definition in rows:
            words = set(_NCS_SEARCH_LEXICON_WORD(_ncs_search_lexicon_text(name)))
            name_df.update(words)
            words.update(_NCS_SEARCH_LEXICON_WORD(_ncs_search_lexicon_text(definition)))
            any_df.update(words)
        self.total = len(rows)
        self._name_words = sorted(name_df)
        self._name_counts = [name_df[word] for word in self._name_words]
        self._any_words = sorted(any_df)
        self._any_counts = [any_df[word] for word in self._any_words]

    @staticmethod
    def _prefix_count(words: list[str], counts: list[int], term: str, cap: int) -> int:
        index = bisect.bisect_left(words, term)
        total = 0
        while index < len(words) and words[index].startswith(term):
            total += counts[index]
            if total >= cap:
                return cap
            index += 1
        return total

    def counts(self, term: str) -> tuple[int, int]:
        key = _ncs_search_lexicon_text(term)
        if not key or not _NCS_SEARCH_LEXICON_WORD(key) == [key]:
            return 0, 0
        return (
            self._prefix_count(self._name_words, self._name_counts, key, self.total),
            self._prefix_count(self._any_words, self._any_counts, key, self.total),
        )


_NCS_SEARCH_LEXICON_WORD = re.compile(r"\w+").findall
_NCS_UNIT_LEXICON_CACHE: dict[tuple[str, int, int, int], _NcsUnitLexicon] = {}
_NCS_UNIT_LEXICON_LOCK = threading.Lock()


def _ncs_search_lexicon_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _ncs_search_unit_lexicon(conn: Any) -> _NcsUnitLexicon:
    """Build once per database file state; in-memory databases always rebuild.

    The key combines the file path, size, modification time, and the highest
    unit rowid, so a snapshot replaced in place or a unit added between calls
    (as tests do) cannot be answered from a stale lexicon.
    """
    key: tuple[str, int, int, int] | None = None
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main" and row[2]:
                stat = os.stat(row[2])
                max_rowid = conn.execute(
                    "SELECT MAX(rowid) FROM competency_units"
                ).fetchone()[0]
                key = (row[2], stat.st_size, stat.st_mtime_ns, int(max_rowid or 0))
    except (OSError, IndexError, TypeError):
        key = None
    if key is not None:
        cached = _NCS_UNIT_LEXICON_CACHE.get(key)
        if cached is not None:
            return cached
    with _NCS_UNIT_LEXICON_LOCK:
        if key is not None and key in _NCS_UNIT_LEXICON_CACHE:
            return _NCS_UNIT_LEXICON_CACHE[key]
        lexicon = _NcsUnitLexicon(
            conn.execute(
                "SELECT unit_name_raw, api_definition FROM competency_units"
            ).fetchall()
        )
        if key is not None:
            # A replaced snapshot gets a new key; keep only the live one.
            _NCS_UNIT_LEXICON_CACHE.clear()
            _NCS_UNIT_LEXICON_CACHE[key] = lexicon
    return lexicon


def _ncs_search_unit_term_counts(
    conn: Any,
    terms: list[str],
    classification_filter: dict[str, str],
    *,
    normalized: bool | str,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Count units whose name, and name or definition, contain each term.

    Both counts use the tiers' lexical-boundary rule.  Without a
    classification filter the cached unit lexicon answers in memory.  Inside a
    filtered scope (a small corpus) one SQL pass applies the same boundary
    UDF after a LIKE prefilter; column expressions come from fixed
    server-side text and terms stay bound.
    """
    terms = list(dict.fromkeys(term for term in terms if term))
    if not terms:
        return 0, {}, {}
    if not classification_filter:
        lexicon = _ncs_search_unit_lexicon(conn)
        name_counts: dict[str, int] = {}
        any_counts: dict[str, int] = {}
        for term in terms:
            name_counts[term], any_counts[term] = lexicon.counts(term)
        return lexicon.total, name_counts, any_counts
    params: dict[str, Any] = {}
    projections: list[str] = []
    for index, term in enumerate(terms):
        # The token_ prefix makes _normalized_ncs_search_params bind both the
        # normalized and raw forms, as the tier predicates do.
        parameter = f"token_probe_{index}"
        params[parameter] = term
        name_match = _ncs_search_boundary_any(
            ("cu.unit_name_raw",), parameter, normalized=normalized
        )
        definition_match = _ncs_search_boundary_any(
            ("cu.api_definition",), parameter, normalized=normalized
        )
        projections.append(f"SUM(CASE WHEN {name_match} THEN 1 ELSE 0 END)")
        projections.append(
            f"SUM(CASE WHEN {name_match} OR {definition_match} THEN 1 ELSE 0 END)"
        )
    if normalized:
        params = _normalized_ncs_search_params(params)
    scope_clause, scope_params = _ncs_classification_filter_sql(
        classification_filter, alias="c", normalized=normalized
    )
    params.update(scope_params)
    row = conn.execute(
        f"SELECT COUNT(*), {', '.join(projections)} FROM competency_units cu "
        "JOIN classifications c ON c.classification_id = cu.classification_id "
        f"WHERE {scope_clause}",
        params,
    ).fetchone()
    name_counts = {}
    any_counts = {}
    for index, term in enumerate(terms):
        name_counts[term] = int(row[1 + index * 2] or 0)
        any_counts[term] = int(row[2 + index * 2] or 0)
    return int(row[0] or 0), name_counts, any_counts


def _ncs_search_idf(total: int, frequency: int) -> float:
    if total <= 1:
        return 1.0
    return max(
        _NCS_SEARCH_IDF_FLOOR,
        math.log(total / max(frequency, 1)) / math.log(total),
    )


def _select_ncs_search_unit_terms(
    conn: Any,
    phrase: str,
    fallback_tokens: list[str],
    classification_filter: dict[str, str],
    *,
    normalized: bool | str,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Resolve query words into unit-corpus terms for fallback ranking.

    Short queries (up to the fallback bound) keep their tokens unless a token
    and all of its stems are absent from the unit corpus.  Such a closed
    compound (``명예퇴직``) is replaced by its head (``퇴직``): the longest
    trailing piece, other than a workflow word, that starts a word in some
    unit name.  Particle forms whose stem exists stay with the morphology fill
    tier, and ``X관리``/``X운영`` compounds stay with the alias-validated
    expander, so short-query behavior only changes where nothing matched.

    Long queries also drop framing words, resolve particles and predicates
    against the corpus (stems first, unit names before definitions), drop
    predicates no unit is named after, fall back to a leading piece when a
    compound has no head (``인력풀`` -> ``인력``), drop workflow words when two
    specific terms remain, and keep at most _NCS_SEARCH_UNIT_TERM_LIMIT terms
    in query order, preferring terms that name a unit and then the rarest.

    Returns the terms, a word -> term trace for response metadata, and the
    words that matched no unit field.  The task/KSA second stage may still
    find those in criteria or KSA text (``직무기술서``).  An empty term list
    means "keep the original tokens".
    """
    words = phrase.split()
    long_query = len(words) > _NCS_SEARCH_FALLBACK_TOKEN_BOUND
    if long_query:
        words = [
            word for word in words
            if len(word) > 1
            and word.casefold() not in _NCS_SEARCH_LONG_QUERY_STOPWORDS
        ]
    else:
        words = list(fallback_tokens)
    words = list(dict.fromkeys(words))
    if not words:
        return [], {}, []
    stems_by_word = {word: _ncs_search_term_stems(word) for word in words}
    probe_terms = [
        term
        for word in words
        for term in (word, *stems_by_word[word])
    ]
    total, name_counts, any_counts = _ncs_search_unit_term_counts(
        conn, probe_terms, classification_filter, normalized=normalized,
    )
    if total <= 1:
        return [], {}, []

    resolved: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for word in words:
        stems = stems_by_word[word]
        if not long_query:
            # Keep present words and particle forms whose stem the morphology
            # fill tier already recovers; only closed compounds move.
            if any_counts.get(word, 0) or any(any_counts.get(stem, 0) for stem in stems):
                resolved.append((word, word))
            else:
                unresolved.append(word)
            continue
        # Prefer a stem over the inflected word (시설의 -> 시설, 보정하고 ->
        # 보정): every field the word matches at a boundary, its prefix stem
        # matches too.  A unit-name occurrence outranks a definition one.
        candidates = (*stems, word)
        term = next(
            (candidate for candidate in candidates if name_counts.get(candidate, 0)),
            "",
        ) or next(
            (candidate for candidate in candidates if any_counts.get(candidate, 0)),
            "",
        )
        if not term:
            unresolved.append(word)
            continue
        if term.casefold() in _NCS_SEARCH_LONG_QUERY_STOPWORDS:
            continue
        if not name_counts.get(term, 0) and term.endswith(_NCS_SEARCH_PREDICATE_FINALS):
            # A predicate or modifier that no unit is named after.
            continue
        resolved.append((word, term))

    if unresolved:
        base_by_word = {
            word: (stems_by_word[word][0] if stems_by_word[word] else word)
            for word in unresolved
        }
        pieces_by_word = {
            word: [
                piece for piece in _ncs_search_compound_pieces(base)
                if piece.casefold() not in _NCS_SEARCH_LONG_QUERY_STOPWORDS
            ]
            for word, base in base_by_word.items()
        }
        _, piece_counts, _ = _ncs_search_unit_term_counts(
            conn,
            [piece for pieces in pieces_by_word.values() for piece in pieces],
            classification_filter,
            normalized=normalized,
        )
        for word in unresolved:
            base = base_by_word[word]
            if _candidate_ncs_search_expansion_bases(base):
                # 채용관리: the trailing workflow noun is not the subject, and
                # short queries already recover the base through the
                # alias-validated expander.  Long queries take the base here.
                piece = next(
                    (
                        candidate
                        for candidate in _candidate_ncs_search_expansion_bases(base)
                        if piece_counts.get(candidate, 0)
                    ),
                    None,
                ) if long_query else None
            else:
                present = [
                    piece for piece in pieces_by_word[word]
                    if piece_counts.get(piece, 0)
                    and piece.casefold() not in _NCS_SEARCH_GENERIC_TOKENS
                ]
                head = next((piece for piece in present if base.endswith(piece)), None)
                modifier = (
                    next((piece for piece in present if base.startswith(piece)), None)
                    if long_query else None
                )
                piece = head or modifier
            if piece:
                resolved.append((word, piece))
            elif not long_query:
                # Nothing better is known; keep the caller's token.
                resolved.append((word, word))

    order = {word: index for index, word in enumerate(words)}
    resolved.sort(key=lambda pair: order[pair[0]])
    terms: list[str] = []
    trace: dict[str, str] = {}
    for word, term in resolved:
        if term not in terms:
            terms.append(term)
        trace.setdefault(word, term)
    if long_query:
        # Workflow words (작성, 관리, 계획) name hundreds of units; in a
        # sentence they let 보고서 작성 outrank the subject.  Keep them only
        # when fewer than two specific terms would remain.
        specific = [
            term for term in terms
            if term.casefold() not in _NCS_SEARCH_GENERIC_TOKENS
        ]
        if len(specific) >= 2:
            terms = specific
            trace = {word: term for word, term in trace.items() if term in terms}
    if long_query and len(terms) > _NCS_SEARCH_UNIT_TERM_LIMIT:
        uncounted = [term for term in terms if term not in any_counts]
        if uncounted:
            _, extra_names, extra_any = _ncs_search_unit_term_counts(
                conn, uncounted, classification_filter, normalized=normalized,
            )
            name_counts.update(extra_names)
            any_counts.update(extra_any)

        # Official unit names are noun phrases, so a term some unit is named
        # with is a subject; a definition-only word may still be a verb.
        rank_key = {
            term: (
                not name_counts.get(term, 0),
                -_ncs_search_idf(total, any_counts.get(term, 0)),
                index,
            )
            for index, term in enumerate(terms)
        }
        ranked = sorted(terms, key=rank_key.__getitem__)
        keep = set(ranked[:_NCS_SEARCH_UNIT_TERM_LIMIT])
        terms = [term for term in terms if term in keep]
        trace = {word: term for word, term in trace.items() if term in keep}
    if not terms or terms == list(fallback_tokens):
        return [], {}, []
    evidence_words = [
        stems_by_word[word][0] if stems_by_word[word] else word
        for word in unresolved
    ]
    evidence_words = [
        word for word in dict.fromkeys(evidence_words)
        if word not in terms and len(word) > 1
    ]
    return terms, trace, evidence_words


def _ncs_search_boundary_match(value: Any, needle: Any) -> int:
    """Return 1 when ``needle`` starts at a lexical boundary in ``value``.

    NCS names are Korean compounds, so a right-hand boundary would reject
    useful prefix matches such as ``데이터분석`` -> ``데이터분석 실무``.  The
    important false-positive case is a query token occurring *inside* another
    word (for example ``차량`` in ``철도차량``), which is rejected by requiring
    a non-word character on the left unless the match starts at position 0.
    The helper is deliberately small and deterministic so it can be registered
    as a SQLite UDF for every search connection and reused by Python metadata.
    """
    normalized_value = _normalize_ncs_search_text(value).casefold()
    normalized_needle = _normalize_ncs_search_text(needle).casefold()
    return _ncs_search_boundary_match_normalized(normalized_value, normalized_needle)


def _ncs_search_boundary_match_normalized(value: Any, needle: Any) -> int:
    """Check already-normalized fields without repeating Unicode conversion."""
    normalized_value = str(value or "")
    normalized_needle = str(needle or "")
    if not normalized_value or not normalized_needle:
        return 0
    start = 0
    while True:
        index = normalized_value.find(normalized_needle, start)
        if index < 0:
            return 0
        if index == 0 or not _ncs_search_word_character(normalized_value[index - 1]):
            return 1
        start = index + 1


def _ncs_search_word_character(character: str) -> bool:
    """Whether a character belongs to a lexical token for boundary checks."""
    if not character:
        return False
    category = unicodedata.category(character)
    return character == "_" or category[0] in {"L", "N", "M"}


def _register_ncs_search_udfs(conn: Any) -> None:
    """Install search UDFs on a connection before executing tier SQL."""
    conn.create_function("ncs_search_match", 2, _ncs_search_boundary_match)
    conn.create_function(
        "ncs_search_match_normalized", 2, _ncs_search_boundary_match_normalized
    )
    conn.create_function("ncs_search_normalize", 1, normalize_search_text)


def _has_normalized_search_columns(conn: Any) -> bool:
    """Trust only a complete, Builder-attested normalized projection."""
    return bool(_normalized_search_storage(conn))


def _normalized_search_storage(conn: Any) -> str | bool:
    """Select an entire attested storage contract, or the all-legacy path.

    Manifest identity is checked before schema so a partial v2 cannot silently
    borrow v1 columns, and contradictory/duplicate attestations fail closed.
    """
    manifest_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(serving_snapshot_manifest)")
    }
    if not {"manifest_key", "manifest_value"}.issubset(manifest_columns):
        return False
    placeholders = ", ".join(
        "?" for _ in SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST
    )
    rows = conn.execute(
        "SELECT manifest_key, manifest_value "
        "FROM serving_snapshot_manifest "
        f"WHERE manifest_key IN ({placeholders})",
        tuple(SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST),
    ).fetchall()
    values = dict(rows)
    if len(values) != len(rows):
        return False
    if values.get("search_normalization_schema") == (
        SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST["search_normalization_schema"]
    ):
        mode = "v2"
        fields = SEARCH_NORMALIZATION_V2_FIELDS
        manifest = SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST
    else:
        mode = "v1"
        fields = SEARCH_NORMALIZATION_FIELDS
        manifest = SEARCH_NORMALIZATION_REQUIRED_MANIFEST
        if "search_normalization_storage" in values:
            return False
    if any(values.get(key) != expected for key, expected in manifest.items()):
        return False
    for table, mapping in fields.items():
        columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        required = set(mapping.values()) | set(SEARCH_NORMALIZATION_SOURCE_FIELDS[table])
        if not required.issubset(columns):
            return False
        if mode == "v2":
            overrides = set(SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {}).values())
            for derived in mapping.values():
                info = columns[derived]
                if str(info[2]).upper() != "TEXT" or bool(info[3]) != (derived not in overrides):
                    return False
    return mode


def _ncs_search_column(column: str, normalized: bool | str) -> str:
    if not normalized:
        return column
    alias, field = column.split(".", 1)
    tables = {
        "cu": "competency_units", "ce": "competency_elements",
        "pc": "performance_criteria", "ki": "ksa_items",
        "c": "classifications", "aliases": "ncs_query_aliases",
    }
    table = tables.get(alias, "")
    if normalized == "v2":
        override = SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {}).get(field)
        if override:
            return f"COALESCE({alias}.{override}, {column}, '')"
    fields = (
        SEARCH_NORMALIZATION_V2_FIELDS
        if normalized == "v2" else SEARCH_NORMALIZATION_FIELDS
    )
    derived = fields.get(table, {}).get(field)
    return f"{alias}.{derived}" if derived else column


def _normalize_ncs_classification_filter(
    classification_filter: dict[str, Any] | None,
) -> dict[str, str]:
    """Keep only explicit, parameter-bound classification constraints."""
    if not isinstance(classification_filter, dict):
        return {}
    normalized: dict[str, str] = {}
    for field in _NCS_CLASSIFICATION_FILTER_FIELDS:
        value = classification_filter.get(field)
        if value is None:
            continue
        text = _normalize_ncs_search_text(value)
        if text:
            normalized[field] = text
    return normalized


def _ignored_ncs_classification_filter_keys(
    classification_filter: dict[str, Any] | None,
) -> tuple[list[str], int]:
    if not isinstance(classification_filter, dict):
        return [], 0

    def bounded_key(key: Any) -> str:
        text = str(key)
        if len(text) <= _NCS_IGNORED_FILTER_KEY_MAX_LENGTH:
            return text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        prefix_length = _NCS_IGNORED_FILTER_KEY_MAX_LENGTH - len(digest) - 1
        return f"{text[:prefix_length]}#{digest}"

    ignored = sorted(
        bounded_key(key)
        for key in classification_filter
        if str(key) not in _NCS_CLASSIFICATION_FILTER_FIELDS
    )
    preview = ignored[:_NCS_IGNORED_FILTER_KEY_PREVIEW_LIMIT]
    return preview, max(0, len(ignored) - len(preview))


def _ncs_context_candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "major_code",
            "middle_code",
            "small_code",
            "sub_code",
            "path_label",
            "confidence",
            "match_basis",
        )
    }


def _ncs_context_candidate_compatible(
    candidate: dict[str, Any],
    classification_filter: dict[str, str],
) -> bool:
    if not classification_filter:
        return True
    members = candidate.get("_members") or []
    for member in members:
        compatible = True
        for field, expected in classification_filter.items():
            actual = str(member.get(field) or "")
            if field.endswith("_code"):
                if actual.casefold() != str(expected).casefold():
                    compatible = False
                    break
            elif not _ncs_search_boundary_match(actual, expected):
                compatible = False
                break
        if compatible:
            return True
    return False


def _ncs_search_scope_invariant(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    classification_filter: dict[str, str],
    *,
    normalized: bool | str = False,
) -> dict[str, Any] | None:
    """Verify every fetched candidate remains inside the effective scope.

    SQL predicates are the first line of containment.  This second line runs
    after tier execution and reranking, while the private code map and full
    source path are still available, so a malformed/custom executor cannot
    leak a row after the response strips the private code map.
    """
    if not classification_filter:
        return None
    checked_rows = 0
    mismatches: list[dict[str, Any]] = []
    for item_type, candidates in candidates_by_type.items():
        for candidate in candidates:
            checked_rows += 1
            mismatch: dict[str, Any] | None = None
            for field, expected in classification_filter.items():
                if field.endswith("_code"):
                    actual = (candidate.get("_classification_codes") or {}).get(field)
                else:
                    # The public path is already required for every leaf and
                    # unit result; reuse it instead of duplicating name fields
                    # on every private candidate object.
                    path_field = field.removesuffix("_name")
                    actual = (candidate.get("path") or {}).get(path_field)
                if field.endswith("_code"):
                    matches = str(actual or "").casefold() == str(expected).casefold()
                elif normalized:
                    matches = (
                        _ncs_search_boundary_match_normalized(
                            normalize_search_text(actual),
                            normalize_search_text(expected),
                        )
                        == 1
                    )
                else:
                    matches = _ncs_search_boundary_match(actual, expected) == 1
                if not matches:
                    mismatch = {
                        "type": item_type,
                        "id": candidate.get("id"),
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                    break
            if mismatch is not None:
                mismatches.append(mismatch)
    if not mismatches:
        return {
            "schema": "ncs_search_classification_scope_invariant_v1",
            "ok": True,
            "status": "verified",
            "checked_rows": checked_rows,
        }
    return {
        "schema": "ncs_search_classification_scope_invariant_v1",
        "ok": False,
        "status": "failed_closed",
        "checked_rows": checked_rows,
        "violation": "classification_filter_post_query_mismatch",
        "mismatches": mismatches[:5],
        "mismatch_count": len(mismatches),
    }


def _ncs_exact_classification_scope_candidates(
    conn: Any,
    normalized_job_scope: str,
    *,
    normalized: bool | str = False,
) -> list[dict[str, Any]]:
    """Return candidates for an exact classification-label scope only.

    This deliberately does not join ``competency_units``.  A unit-name hit is
    therefore never promoted to an exact classification scope; callers can
    fall back to the legacy resolver when this query produces no candidates.
    The normalized projection is used when its manifest is attested.  On
    legacy databases, scanning the small classifications table still avoids
    the expensive per-classification unit-name aggregation.
    """
    levels = ("major", "middle", "small", "sub")
    query_value = normalize_search_text(normalized_job_scope)
    if not query_value:
        return []

    selected_fields = (
        "classification_id, major_code, major_name, middle_code, middle_name, "
        "small_code, small_name, sub_code, sub_name"
    )
    if normalized:
        predicates = [
            f"{_ncs_search_column(f'c.{level}_name', normalized)} = ?"
            for level in levels
        ]
        rows = conn.execute(
            f"SELECT {selected_fields} FROM classifications c "
            f"WHERE {' OR '.join(predicates)} "
            "ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, "
            "c.classification_id",
            (query_value,) * len(predicates),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {selected_fields} FROM classifications c "
            "ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, "
            "c.classification_id"
        ).fetchall()

    candidates: dict[tuple[str | None, ...], dict[str, Any]] = {}
    for row in rows:
        matching_depths = [
            depth
            for depth, level in enumerate(levels)
            if normalize_search_text(row[f"{level}_name"]) == query_value
        ]
        if not matching_depths:
            continue
        # A row can repeat a label at multiple hierarchy levels.  The deepest
        # exact node on that canonical branch is the only useful candidate.
        depth = max(matching_depths)
        codes = tuple(
            str(row[f"{level}_code"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        names = tuple(
            str(row[f"{level}_name"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        key = codes
        candidate = candidates.setdefault(
            key,
            {
                **{f"{level}_code": codes[index] for index, level in enumerate(levels)},
                **{f"{level}_name": names[index] for index, level in enumerate(levels)},
                "path_label": " > ".join(name for name in names if name),
                "confidence": 1.0,
                "match_basis": [f"job_scope_exact_{levels[depth]}_name"],
                "_depth": depth,
                "_job_basis": 1.0,
                "_job_match_name": query_value,
                "_context_token_count": 0,
                "_members": [],
            },
        )
        candidate["_members"].append({key: row[key] for key in row.keys()})
    return list(candidates.values())


def _ncs_prune_exact_scope_candidates(
    candidates: list[dict[str, Any]],
    classification_filter: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep deepest same-branch exact nodes and return compatible paths."""
    levels = ("major", "middle", "small", "sub")
    ranked = sorted(
        candidates,
        key=lambda item: (
            -int(item["_depth"]),
            str(item.get("major_code") or ""),
            str(item.get("middle_code") or ""),
            str(item.get("small_code") or ""),
            str(item.get("sub_code") or ""),
        ),
    )

    def prune(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pruned: list[dict[str, Any]] = []
        for candidate in branches:
            depth = int(candidate["_depth"])
            if any(
                depth < int(kept["_depth"])
                and all(
                    candidate.get(f"{levels[index]}_code")
                    == kept.get(f"{levels[index]}_code")
                    for index in range(depth + 1)
                )
                for kept in pruned
            ):
                continue
            pruned.append(candidate)
        return pruned

    # Keep an unfiltered pruned view for fail-closed conflict metadata, while
    # selecting the deepest compatible node when a caller filter narrows an
    # otherwise duplicated exact label to one branch.
    pruned = prune(ranked)
    compatible = prune([
        item for item in ranked
        if _ncs_context_candidate_compatible(item, classification_filter)
    ])
    return compatible, pruned


def _ncs_resolve_exact_classification_scope(
    conn: Any,
    *,
    normalized_job_scope: str,
    normalized_filter: dict[str, str],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve an exact classification-only scope, or signal legacy fallback."""
    normalized = _normalized_search_storage(conn)
    candidates = _ncs_exact_classification_scope_candidates(
        conn,
        normalized_job_scope,
        normalized=normalized,
    )
    if not candidates:
        return None

    compatible, pruned = _ncs_prune_exact_scope_candidates(
        candidates,
        normalized_filter,
    )
    if not compatible:
        top = pruned[0]
        base["selected_candidate"] = _ncs_context_candidate_public(top)
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in pruned[1:4]
        ]
        base["alternative_count"] = max(0, len(pruned) - 1)
        # No candidate survives the hard filter, so a numeric score margin is
        # not meaningful; zero is the explicit fail-closed contract value.
        base["resolution_margin"] = 0.0
        base.update(status="conflict", needs_context=True)
        base["warnings"].append("context_conflicts_with_hard_filter")
        return base
    if len(compatible) != 1:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in compatible[:3]
        ]
        base["alternative_count"] = len(compatible)
        base["resolution_margin"] = 0.0
        base.update(status="ambiguous", needs_context=True)
        return base

    selected = compatible[0]
    base["selected_candidate"] = _ncs_context_candidate_public(selected)
    base["alternative_candidates"] = []
    base["alternative_count"] = 0
    base["resolution_margin"] = 1.0
    base.update(status="resolved", needs_context=False)
    return base


def _ncs_resolve_exact_unit_scope(
    conn: Any,
    *,
    normalized_job_scope: str,
    normalized_filter: dict[str, str],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve an exact official competency-unit name to its full path.

    Classification labels are attempted first.  When the caller supplies a
    unit title (for example ``사회복지조직 인사관리`` or ``수출입계약``), an
    exact unit match is stronger than a prefix hit on a broad classification
    label (``사회복지`` or ``인사``).  Element names are deliberately not
    promoted here because an element can be shared by unrelated units and
    must remain a lexical result unless its parent scope is explicit.
    """
    normalized = _normalized_search_storage(conn)
    levels = ("major", "middle", "small", "sub")
    query_value = normalize_search_text(normalized_job_scope)
    if not query_value:
        return None
    unit_column = _ncs_search_column("cu.unit_name_raw", normalized)
    where_sql = f"WHERE {unit_column} = ?" if normalized else ""
    params = (query_value,) if normalized else ()
    rows = conn.execute(
        "SELECT cu.unit_code, cu.unit_name_raw, "
        "c.classification_id, c.major_code, c.major_name, "
        "c.middle_code, c.middle_name, c.small_code, c.small_name, "
        "c.sub_code, c.sub_name "
        "FROM competency_units cu "
        "JOIN classifications c ON c.classification_id = cu.classification_id "
        f"{where_sql} "
        "ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, "
        "cu.unit_code",
        params,
    ).fetchall()
    if not normalized:
        rows = [row for row in rows if normalize_search_text(row["unit_name_raw"]) == query_value]
    if not rows:
        return None

    candidates: dict[tuple[str | None, ...], dict[str, Any]] = {}
    for row in rows:
        codes = tuple(str(row[f"{level}_code"] or "") or None for level in levels)
        names = tuple(str(row[f"{level}_name"] or "") or None for level in levels)
        key = codes
        candidate = candidates.setdefault(
            key,
            {
                **{f"{level}_code": codes[index] for index, level in enumerate(levels)},
                **{f"{level}_name": names[index] for index, level in enumerate(levels)},
                "path_label": " > ".join(name for name in names if name),
                "confidence": 1.0,
                "match_basis": ["job_scope_exact_unit_name"],
                "_depth": len(levels) - 1,
                "_job_basis": 1.0,
                "_job_match_name": query_value,
                "_context_token_count": 0,
                "_members": [],
            },
        )
        candidate["_members"].append(dict(row))
    compatible, pruned = _ncs_prune_exact_scope_candidates(
        list(candidates.values()), normalized_filter
    )
    if not compatible:
        top = pruned[0]
        base["selected_candidate"] = _ncs_context_candidate_public(top)
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in pruned[1:4]
        ]
        base["alternative_count"] = max(0, len(pruned) - 1)
        base["resolution_margin"] = 0.0
        base.update(status="conflict", needs_context=True)
        base["warnings"].append("context_conflicts_with_hard_filter")
        return base
    if len(compatible) != 1:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in compatible[:3]
        ]
        base["alternative_count"] = len(compatible)
        base["resolution_margin"] = 0.0
        base.update(status="ambiguous", needs_context=True)
        return base
    base["selected_candidate"] = _ncs_context_candidate_public(compatible[0])
    base["alternative_candidates"] = []
    base["alternative_count"] = 0
    base["resolution_margin"] = 1.0
    base.update(status="resolved", needs_context=False)
    return base


def resolve_ncs_search_context(
    conn: Any,
    *,
    context_text: Any = None,
    job_scope: Any = None,
    classification_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve caller-supplied HR context against source-backed NCS paths.

    The search query is intentionally absent from this API.  Resolution is
    read-only and produces a shadow prior only; it never becomes a SQL filter
    or changes the public result order during rollout phase 2.
    """
    normalized_context, normalized_job_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    normalized_filter = _normalize_ncs_classification_filter(classification_filter)
    ignored_filter_keys, ignored_filter_key_omitted_count = (
        _ignored_ncs_classification_filter_keys(classification_filter)
    )
    requested = search_context_request_contract(
        context_text=normalized_context,
        job_scope=normalized_job_scope,
        classification_filter=normalized_filter,
    )
    policy = {
        "query_inference_allowed": False,
        "soft_prior_source": (
            "caller_supplied_context"
            if normalized_context or normalized_job_scope
            else None
        ),
        "hard_filter_source": "caller_supplied" if normalized_filter else None,
        "lexical_tier_preserved": True,
        "rollout_phase": "shadow",
    }
    warnings = [
        f"ignored_classification_filter_key:{key}"
        for key in ignored_filter_keys
    ]
    if ignored_filter_key_omitted_count:
        warnings.append(
            "ignored_classification_filter_keys_omitted:"
            f"{ignored_filter_key_omitted_count}"
        )
    base = {
        "schema": NCS_SEARCH_CONTEXT_SCHEMA,
        "resolver_version": NCS_SEARCH_CONTEXT_RESOLVER_VERSION,
        "requested": requested,
        "policy": policy,
        "selected_candidate": None,
        "alternative_candidates": [],
        "alternative_count": 0,
        "prior_applied": False,
        "shadow_mode": True,
        "shadow_ranking_computed": False,
        "hard_filter_applied": bool(normalized_filter),
        "needs_context": False,
        "warnings": warnings,
    }
    if not normalized_context and not normalized_job_scope:
        base["status"] = "filtered" if normalized_filter else "not_provided"
        return base

    # A classification-only exact label is safe to resolve without scanning
    # and aggregating every unit name.  Any miss falls through to the existing
    # resolver so unit-name, boundary, and fuzzy semantics remain unchanged.
    if normalized_job_scope and not normalized_context:
        exact = _ncs_resolve_exact_classification_scope(
            conn,
            normalized_job_scope=normalized_job_scope,
            normalized_filter=normalized_filter,
            base=base,
        )
        if exact is not None:
            return exact
        exact_unit = _ncs_resolve_exact_unit_scope(
            conn,
            normalized_job_scope=normalized_job_scope,
            normalized_filter=normalized_filter,
            base=base,
        )
        if exact_unit is not None:
            return exact_unit

    rows = conn.execute(
        """
        SELECT c.classification_id,
               c.major_code, c.major_name,
               c.middle_code, c.middle_name,
               c.small_code, c.small_name,
               c.sub_code, c.sub_name,
               GROUP_CONCAT(COALESCE(cu.unit_name_raw, ''), ' ') AS unit_names
        FROM classifications c
        LEFT JOIN competency_units cu
          ON cu.classification_id = c.classification_id
        GROUP BY c.classification_id,
                 c.major_code, c.major_name,
                 c.middle_code, c.middle_name,
                 c.small_code, c.small_name,
                 c.sub_code, c.sub_name
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code,
                 c.classification_id
        """
    ).fetchall()
    if not rows:
        base.update(status="unresolved", needs_context=True)
        base["warnings"].append("classification_corpus_empty")
        return base

    levels = ("major", "middle", "small", "sub")
    context_tokens = list(
        dict.fromkeys(
            token
            for token in normalize_search_text(normalized_context).split()
            if len(token) >= 2
        )
    )[:12]
    row_documents: list[str] = []
    for row in rows:
        row_documents.append(
            " ".join(
                normalize_search_text(row[f"{level}_name"])
                for level in levels
                if row[f"{level}_name"]
            )
            + " "
            + normalize_search_text(row["unit_names"])
        )
    context_weights: dict[str, float] = {}
    for token in context_tokens:
        frequency = sum(
            1 for document in row_documents
            if _ncs_search_boundary_match_normalized(document, token)
        )
        context_weights[token] = math.log((len(rows) + 1) / (frequency + 1)) + 1.0
    total_context_weight = sum(context_weights.values())

    candidates: dict[tuple[str | None, ...], dict[str, Any]] = {}

    def add_candidate(
        row: Any,
        *,
        depth: int,
        job_basis: float,
        job_match_name: str | None,
        job_match_basis: str | None,
        matched_context_tokens: list[str],
    ) -> None:
        codes = tuple(
            str(row[f"{level}_code"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        names = tuple(
            str(row[f"{level}_name"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        matched_weight = sum(
            context_weights.get(token, 0.0) for token in matched_context_tokens
        )
        coverage = (
            matched_weight / total_context_weight if total_context_weight else 0.0
        )
        if normalized_job_scope and normalized_context:
            score = min(1.0, 0.8 * job_basis + 0.2 * coverage)
        elif normalized_job_scope:
            score = job_basis
        else:
            score = min(0.6, 0.6 * coverage)
        if score <= 0:
            return
        key = codes
        path_label = " > ".join(name for name in names if name)
        candidate = candidates.setdefault(
            key,
            {
                **{f"{level}_code": codes[index] for index, level in enumerate(levels)},
                **{f"{level}_name": names[index] for index, level in enumerate(levels)},
                "path_label": path_label,
                "confidence": 0.0,
                "match_basis": [],
                "_depth": depth,
                "_job_basis": 0.0,
                "_job_match_name": job_match_name,
                "_context_token_count": 0,
                "_members": [],
            },
        )
        candidate["_members"].append({key: row[key] for key in row.keys()})
        candidate["_job_basis"] = max(candidate["_job_basis"], job_basis)
        if job_match_name:
            candidate["_job_match_name"] = job_match_name
        candidate["_context_token_count"] = max(
            candidate["_context_token_count"], len(matched_context_tokens)
        )
        candidate["confidence"] = max(candidate["confidence"], round(score, 4))
        bases = candidate["match_basis"]
        if job_match_basis and job_match_basis not in bases:
            bases.append(job_match_basis)
        if matched_context_tokens:
            # Context text is sensitive caller input.  The response may expose
            # that token overlap contributed, but never the matching tokens.
            basis = "context_text_token_overlap"
            if basis not in bases:
                bases.append(basis)

    normalized_job = normalize_search_text(normalized_job_scope)
    for row, document in zip(rows, row_documents):
        matched_context = [
            token
            for token in context_tokens
            if _ncs_search_boundary_match_normalized(document, token)
        ]
        job_depth = 3
        job_basis = 0.0
        job_match_name: str | None = None
        job_match_basis: str | None = None
        if normalized_job:
            exact_matches: list[tuple[int, str]] = []
            boundary_matches: list[tuple[int, str]] = []
            for depth, level in enumerate(levels):
                name = normalize_search_text(row[f"{level}_name"])
                if not name:
                    continue
                if name == normalized_job:
                    exact_matches.append((depth, name))
                elif (
                    # Scope promotion requires a complete lexical match.  A
                    # one-sided prefix would incorrectly map an element such
                    # as ``인사하기`` to the unrelated classification ``인사``.
                    _ncs_search_boundary_match_normalized(name, normalized_job)
                    and _ncs_search_boundary_match_normalized(normalized_job, name)
                ):
                    boundary_matches.append((depth, name))
            if exact_matches:
                job_depth, job_match_name = max(exact_matches)
                job_basis = 1.0
                job_match_basis = f"job_scope_exact_{levels[job_depth]}_name"
            elif boundary_matches:
                job_depth, job_match_name = max(boundary_matches)
                job_basis = 0.9
                job_match_basis = f"job_scope_boundary_{levels[job_depth]}_name"
            else:
                unit_names = normalize_search_text(row["unit_names"])
                if unit_names and _ncs_search_boundary_match_normalized(
                    unit_names, normalized_job
                ):
                    job_basis = 0.75
                    job_match_name = normalized_job
                    job_match_basis = "job_scope_official_unit_name"
        if normalized_job and not job_basis and not matched_context:
            continue
        if not normalized_job and not matched_context:
            continue
        add_candidate(
            row,
            depth=job_depth if job_basis else 3,
            job_basis=job_basis,
            job_match_name=job_match_name,
            job_match_basis=job_match_basis,
            matched_context_tokens=matched_context,
        )

    candidate_values = list(candidates.values())
    if normalized_job and any(
        float(item["_job_basis"]) >= 1.0 for item in candidate_values
    ):
        # An exact source-backed job-scope name is stronger than context-text
        # coverage on a broader boundary match.  Context may disambiguate two
        # exact names, but cannot demote the only exact hierarchy node.
        candidate_values = [
            item for item in candidate_values if float(item["_job_basis"]) >= 1.0
        ]
    ranked = sorted(
        candidate_values,
        key=lambda item: (
            -float(item["confidence"]),
            -int(item["_depth"]),
            str(item.get("major_code") or ""),
            str(item.get("middle_code") or ""),
            str(item.get("small_code") or ""),
            str(item.get("sub_code") or ""),
        ),
    )
    # When the same exact name appears on an ancestor and its descendant in the
    # same path (for example 총무), keep the most specific source-backed node.
    pruned: list[dict[str, Any]] = []
    for candidate in ranked:
        is_ancestor_duplicate = any(
            int(candidate["_depth"]) < int(kept["_depth"])
            and float(candidate["confidence"]) <= float(kept["confidence"])
            and all(
                candidate.get(f"{levels[index]}_code")
                == kept.get(f"{levels[index]}_code")
                for index in range(int(candidate["_depth"]) + 1)
            )
            for kept in pruned
        )
        if not is_ancestor_duplicate:
            pruned.append(candidate)
    ranked = pruned
    if not ranked:
        base.update(status="unresolved", needs_context=True)
        return base

    top = ranked[0]
    second_score = float(ranked[1]["confidence"]) if len(ranked) > 1 else 0.0
    margin = round(float(top["confidence"]) - second_score, 4)
    if normalized_job_scope:
        threshold_ok = float(top["confidence"]) >= 0.8
        margin_ok = margin >= 0.15
    else:
        threshold_ok = (
            float(top["confidence"]) >= 0.5
            and int(top["_context_token_count"]) >= 2
        )
        margin_ok = margin >= 0.2
    base["resolution_margin"] = margin
    if not threshold_ok:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in ranked[:3]
        ]
        base["alternative_count"] = len(ranked)
        base.update(status="unresolved", needs_context=True)
        return base
    if not margin_ok:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in ranked[:3]
        ]
        base["alternative_count"] = len(ranked)
        base.update(status="ambiguous", needs_context=True)
        return base
    selected = _ncs_context_candidate_public(top)
    base["selected_candidate"] = selected
    base["alternative_candidates"] = [
        _ncs_context_candidate_public(item) for item in ranked[1:4]
    ]
    base["alternative_count"] = max(0, len(ranked) - 1)
    if normalized_filter and not _ncs_context_candidate_compatible(
        top, normalized_filter
    ):
        base.update(status="conflict", needs_context=True)
        base["warnings"].append("context_conflicts_with_hard_filter")
        return base
    base.update(status="resolved", needs_context=False)
    return base


def _ncs_context_affinity(
    classification_codes: dict[str, Any],
    selected_candidate: dict[str, Any] | None,
) -> tuple[float, str | None]:
    if not selected_candidate:
        return 0.0, None
    weights = {"major": 0.4, "middle": 0.6, "small": 0.8, "sub": 1.0}
    matched_level: str | None = None
    for level in ("major", "middle", "small", "sub"):
        selected = selected_candidate.get(f"{level}_code")
        if selected is None:
            break
        if str(classification_codes.get(f"{level}_code") or "") != str(selected):
            return 0.0, None
        matched_level = level
    return (weights.get(matched_level, 0.0), matched_level)


def _annotate_ncs_search_shadow(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    requested_types: tuple[str, ...],
    search_context: dict[str, Any],
) -> None:
    selected = (
        search_context.get("selected_candidate")
        if search_context.get("status") == "resolved"
        else None
    )
    for item_type in requested_types:
        candidates = candidates_by_type.get(item_type, [])
        annotated: list[tuple[int, float]] = []
        for baseline_rank, item in enumerate(candidates, start=1):
            affinity, level = _ncs_context_affinity(
                item.get("_classification_codes") or {}, selected
            )
            item["context_affinity"] = affinity
            item["context_match"] = {
                "level": level,
                "matched": bool(affinity),
                "prior_applied": False,
            }
            annotated.append((baseline_rank, affinity))
        shadow_order = sorted(annotated, key=lambda pair: (-pair[1], pair[0]))
        shadow_rank = {
            baseline_rank: rank
            for rank, (baseline_rank, _) in enumerate(shadow_order, start=1)
        }
        for baseline_rank, item in enumerate(candidates, start=1):
            item["shadow_rank"] = shadow_rank[baseline_rank]
    search_context["shadow_ranking_computed"] = bool(
        selected and any(candidates_by_type.get(item_type) for item_type in requested_types)
    )


def _ncs_search_needs_context(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    selected_tier_by_type: dict[str, int | None],
) -> bool:
    if selected_tier_by_type.get("unit") != 3:
        return False
    candidates = candidates_by_type.get("unit", [])[:5]
    if not candidates:
        return False
    scope_counts: dict[tuple[str, str, str, str], int] = {}
    for item in candidates:
        codes = item.get("_classification_codes") or {}
        key = tuple(
            str(codes.get(f"{level}_code") or "")
            for level in ("major", "middle", "small", "sub")
        )
        scope_counts[key] = scope_counts.get(key, 0) + 1
    return bool(
        len(scope_counts) >= 2
        and max(scope_counts.values()) / len(candidates) < 0.6
    )


def _ncs_classification_filter_sql(
    classification_filter: dict[str, str],
    *,
    alias: str = "c",
    normalized: bool | str = False,
) -> tuple[str, dict[str, str]]:
    """Build exact-code/boundary-name predicates for a classification alias."""
    clauses: list[str] = []
    params: dict[str, str] = {}
    for field in _NCS_CLASSIFICATION_FILTER_FIELDS:
        value = classification_filter.get(field)
        if not value:
            continue
        parameter = f"class_filter_{field}"
        params[parameter] = value
        if field.endswith("_code"):
            clauses.append(
                f"TRIM(COALESCE({alias}.{field}, '')) = :{parameter} COLLATE NOCASE"
            )
        else:
            column = _ncs_search_column(f"{alias}.{field}", normalized)
            function = "ncs_search_match_normalized" if normalized else "ncs_search_match"
            if normalized:
                params[parameter] = normalize_search_text(value)
            clauses.append(
                f"{function}(COALESCE({column}, ''), :{parameter}) = 1"
            )
    if not clauses:
        return "", {}
    return "(" + " AND ".join(clauses) + ")", params


def _apply_ncs_classification_filter_to_tiers(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    classification_filter: dict[str, str],
    *,
    normalized: bool | str = False,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    clause, filter_params = _ncs_classification_filter_sql(
        classification_filter, normalized=normalized
    )
    if not clause:
        return tiers
    filtered: list[tuple[int, str, dict[str, Any], str, str]] = []
    for match_tier, where_clause, params, score_clause, meaningful_clause in tiers:
        tier_params = dict(params)
        tier_params.update(filter_params)
        filtered.append(
            (
                match_tier,
                f"({where_clause}) AND {clause}",
                tier_params,
                score_clause,
                meaningful_clause,
            )
        )
    return filtered


def _ncs_search_intent_expansions(phrase: str) -> list[str]:
    """Return deduplicated official terms for strong practitioner-language hints."""
    normalized_phrase = phrase.casefold()
    expansions: list[str] = []
    for trigger, alternatives in _NCS_SEARCH_QUERY_INTENT_EQUIVALENTS.items():
        if trigger.casefold() not in normalized_phrase:
            continue
        blockers = _NCS_SEARCH_QUERY_INTENT_BLOCKERS.get(trigger, ())
        if any(blocker.casefold() in normalized_phrase for blocker in blockers):
            continue
        for alternative in alternatives:
            if alternative not in expansions:
                expansions.append(alternative)
    return expansions


def _ncs_search_has_exact_unit_name(
    conn: Any,
    phrase: str,
    classification_filter: dict[str, str],
    *,
    normalized: bool | str = False,
) -> bool:
    """Keep a scoped official name ahead of practitioner-language rewrites."""
    name_column = _ncs_search_column("cu.unit_name_raw", normalized)
    if not normalized:
        name_column = f"ncs_search_normalize({name_column})"
    clause, params = _ncs_classification_filter_sql(
        classification_filter, normalized=normalized
    )
    params["literal_name"] = normalize_search_text(phrase)
    return conn.execute(
        "SELECT 1 FROM competency_units cu JOIN classifications c "
        "ON c.classification_id = cu.classification_id "
        f"WHERE {name_column} = :literal_name"
        + (f" AND {clause}" if clause else "") + " LIMIT 1",
        params,
    ).fetchone() is not None


def _escape_ncs_search_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ncs_search_like_any(columns: tuple[str, ...], parameter: str) -> str:
    return "(" + " OR ".join(
        f"COALESCE({column}, '') LIKE :{parameter} ESCAPE '\\'"
        for column in columns
    ) + ")"


def _ncs_search_boundary_any(
    columns: tuple[str, ...], parameter: str, *, normalized: bool | str = False
) -> str:
    """Build a parameter-bound lexical-boundary predicate for SQL search."""
    predicates = []
    for raw_column in columns:
        column = _ncs_search_column(raw_column, normalized)
        derived = normalized and column != raw_column
        function = "ncs_search_match_normalized" if derived else "ncs_search_match"
        bind = f"{parameter}_raw" if normalized and not derived else parameter
        # Keep the cheap SQLite LIKE prefilter ahead of the Python UDF.  The
        # UDF preserves lexical-boundary semantics, while LIKE avoids calling
        # it for the vast majority of rows in the large criteria/KSA tables.
        predicates.append(
            f"(COALESCE({column}, '') LIKE '%' || :{bind} || '%' "
            f"AND {function}(COALESCE({column}, ''), :{bind}) = 1)"
        )
    return "(" + " OR ".join(predicates) + ")"


def _candidate_ncs_search_expansion_bases(token: str) -> list[str]:
    """Return conservative compound bases that still require alias validation."""
    candidates: list[str] = []
    for suffix in _NCS_SEARCH_LOW_INFORMATION_SUFFIXES:
        if not token.endswith(suffix):
            continue
        base = token[: -len(suffix)].strip()
        if len(base) >= 2 and base not in candidates:
            candidates.append(base)
    return candidates


def _ncs_search_morphology_compound_bases(stem: str) -> list[str]:
    """Bound a stripped compound's recall to one non-generic subject noun."""
    return [
        base for base in _candidate_ncs_search_expansion_bases(stem)
        if base.casefold() not in _NCS_SEARCH_GENERIC_TOKENS
    ][:1]


def _validated_ncs_search_token_expansions(
    conn: Any,
    fallback_tokens: list[str],
) -> dict[str, list[str]]:
    """Return code-reviewed and alias-validated recall-only expansions.

    Query aliases are already part of public-search recall.  Both expansion
    sources remain recall-only: this helper never changes review state or treats
    an expansion as source evidence.
    """
    expansions: dict[str, list[str]] = {}
    for token in fallback_tokens:
        alternatives = list(
            _NCS_SEARCH_QUERY_EQUIVALENTS.get(token.casefold(), ())
        )
        if alternatives:
            expansions[token] = alternatives

    candidate_bases_by_token = {
        token: _candidate_ncs_search_expansion_bases(token)
        for token in fallback_tokens
    }
    candidate_bases = sorted(
        {
            base
            for bases in candidate_bases_by_token.values()
            for base in bases
        }
    )
    if not candidate_bases:
        return expansions

    parameters = {
        f"expansion_base_{index}": base
        for index, base in enumerate(candidate_bases)
    }
    placeholders = ", ".join(f":{name}" for name in parameters)
    rows = conn.execute(
        f"""
        SELECT alias_text, normalized_query
        FROM ncs_query_aliases
        WHERE alias_text COLLATE NOCASE IN ({placeholders})
           OR normalized_query COLLATE NOCASE IN ({placeholders})
        """,
        parameters,
    ).fetchall()
    aliases_by_term: dict[str, list[str]] = {}
    for row in rows:
        alias_text = _normalize_ncs_search_text(row["alias_text"])
        normalized_query = _normalize_ncs_search_text(row["normalized_query"])
        values = [value for value in (alias_text, normalized_query) if len(value) >= 2]
        for value in values:
            aliases_by_term.setdefault(value.casefold(), [])
            for candidate in values:
                if candidate not in aliases_by_term[value.casefold()]:
                    aliases_by_term[value.casefold()].append(candidate)

    for token, bases in candidate_bases_by_token.items():
        alternatives = expansions.setdefault(token, [])
        for base in bases:
            linked_terms = aliases_by_term.get(base.casefold())
            if not linked_terms:
                continue
            for alternative in (base, *linked_terms):
                if alternative != token and alternative not in alternatives:
                    alternatives.append(alternative)
        if not alternatives:
            expansions.pop(token, None)
    return expansions


def _ncs_search_leaf_token_expansions(
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Keep job-scope compound reductions out of task/evidence leaf text.

    Alias validation lets a query such as ``인사업무`` recover the scoped
    subject ``인사`` for competency-unit names and classifications.  The same
    short form is unsafe in element, criterion, and KSA text where homographs
    can express an unrelated action (for example a greeting).  Code-reviewed
    equivalents that are not produced by the low-information suffix rule stay
    available to every scope.
    """
    job_scope_tokens = {
        token
        for token in fallback_tokens
        if _candidate_ncs_search_expansion_bases(token)
    }
    return {
        token: alternatives
        for token, alternatives in token_expansions.items()
        if token not in job_scope_tokens
    }


def _ncs_search_token_idf_weights(
    conn: Any,
    fallback_tokens: list[str],
    classification_filter: dict[str, str] | None = None,
    *,
    normalized: bool | str = False,
) -> dict[str, float]:
    """Weight tokens by document frequency in the active search corpus.

    When callers provide a classification filter, the IDF corpus must be the
    same filtered unit set used by the search tiers.  Otherwise a token can be
    common in unrelated NCS majors and be underweighted inside the requested
    scope.  The no-filter path intentionally keeps the original whole-corpus
    query shape and behavior.
    """
    tokens = list(dict.fromkeys(fallback_tokens))
    if not tokens:
        return {}
    # Count the corpus and every token in one pass.  The compact serving
    # profile drops the unit_name_raw index, so each LIKE reads the whole
    # table; a COUNT per token would re-scan it once per token.  Column
    # expressions and placeholder names come from fixed server-side text and
    # integer indexes, and query text stays bound.
    name_column = "unit_name_search_norm" if normalized else "unit_name_raw"
    frequency_terms = ", ".join(
        f"SUM(CASE WHEN {name_column} LIKE :idf_token_{index} ESCAPE '\\' "
        "THEN 1 ELSE 0 END)"
        for index in range(len(tokens))
    )
    params = {
        f"idf_token_{index}": f"%{_escape_ncs_search_like(normalize_search_text(token) if normalized else token)}%"
        for index, token in enumerate(tokens)
    }
    normalized_filter = classification_filter or {}
    scope_clause, scope_params = _ncs_classification_filter_sql(
        normalized_filter,
        alias="c",
        normalized=normalized,
    )
    if scope_clause:
        from_clause = (
            "competency_units cu "
            "JOIN classifications c ON c.classification_id = cu.classification_id"
        )
        sql = f"SELECT COUNT(*), {frequency_terms} FROM {from_clause} WHERE {scope_clause}"
        params = {**params, **scope_params}
    else:
        sql = f"SELECT COUNT(*), {frequency_terms} FROM competency_units"
    row = conn.execute(sql, params).fetchone()
    total = int(row[0] or 0)
    if total <= 1:
        return {}
    ceiling = math.log(total)
    weights: dict[str, float] = {}
    for index, token in enumerate(tokens):
        frequency = max(int(row[index + 1] or 0), 1)
        weights[token] = max(
            _NCS_SEARCH_IDF_FLOOR,
            math.log(total / frequency) / ceiling,
        )
    return weights


def _ncs_search_fallback_ranking(
    weighted_columns: tuple[tuple[str, float], ...],
    fallback_tokens: list[str],
    parameter_groups: list[list[str]],
    search_groups: list[str],
    token_weights: dict[str, float] | None = None,
    *,
    normalized: bool | str = False,
) -> tuple[str, str, dict[str, Any]]:
    """Build a parameter-bound score and a non-generic-hit predicate.

    Column expressions and placeholder names come only from fixed server-side
    tuples and integer indexes.  Query text and weights remain bound parameters.
    """
    score_terms: list[str] = []
    meaningful_groups: list[str] = []
    rank_params: dict[str, Any] = {}
    weights = token_weights or {}
    for token_index, token in enumerate(fallback_tokens):
        is_generic = token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
        if not is_generic:
            meaningful_groups.append(search_groups[token_index])
        # Document frequency subsumes the hand kept list for scoring; the list
        # still decides which tokens may be a sole fallback hit.
        token_factor = weights.get(
            token,
            _NCS_SEARCH_GENERIC_TOKEN_FACTOR if is_generic else 1.0,
        )
        for column_index, (column, field_weight) in enumerate(weighted_columns):
            field_matches = "(" + " OR ".join(
                _ncs_search_boundary_any((column,), parameter, normalized=normalized)
                for parameter in parameter_groups[token_index]
            ) + ")"
            weight_parameter = f"rank_weight_{token_index}_{column_index}"
            rank_params[weight_parameter] = field_weight * token_factor
            score_terms.append(
                f"CASE WHEN {field_matches} "
                f"THEN :{weight_parameter} ELSE 0 END"
            )
    score_clause = " + ".join(score_terms) or "0"
    meaningful_clause = (
        "(" + " OR ".join(meaningful_groups) + ")"
        if meaningful_groups
        else "0 = 1"
    )
    return score_clause, meaningful_clause, rank_params


def _ncs_search_unit_task_ksa_scores(
    conn: Any,
    unit_codes: list[str],
    fallback_tokens: list[str],
    token_weights: dict[str, float] | None = None,
    *,
    normalized: bool | str | None = None,
) -> dict[str, float]:
    """Score task/KSA evidence for an already retrieved unit candidate set.

    This is intentionally a second-stage lookup.  It never scans the full
    criteria/KSA corpus for every query: only unit codes already returned by
    the lexical fallback are fetched through the element indexes.  Evidence
    contributes once per matched query token, avoiding a verbosity bias toward
    units with more criteria rows.
    """
    candidates = list(dict.fromkeys(str(code) for code in unit_codes if code))
    tokens = list(dict.fromkeys(token for token in fallback_tokens if token))
    if not candidates or not tokens:
        return {}
    if normalized is None:
        normalized = _normalized_search_storage(conn)
    parameters = {
        f"task_ksa_unit_{index}": code
        for index, code in enumerate(candidates)
    }
    # Only Builder-normalized columns can safely prefilter the Unicode-aware
    # boundary check. Legacy raw LIKE would discard fullwidth/decomposed/casefold
    # matches before Python sees them, so legacy snapshots fetch the candidate
    # units' evidence without this additional text prefilter.
    for index, token in enumerate(tokens):
        parameters[f"task_ksa_like_{index}"] = (
            f"%{_escape_ncs_search_like(normalize_search_text(token) if normalized else token)}%"
        )
    unit_placeholders = ", ".join(
        f":task_ksa_unit_{index}" for index in range(len(candidates))
    )

    def evidence_filter(column: str) -> str:
        if not normalized:
            return "1 = 1"
        column = _ncs_search_column(column, normalized)
        return "(" + " OR ".join(
            f"COALESCE({column}, '') LIKE :task_ksa_like_{index} ESCAPE '\\'"
            for index in range(len(tokens))
        ) + ")"

    def evidence_projection(column: str) -> str:
        return (
            f", {_ncs_search_column(column, normalized)} AS evidence_match_text"
            if normalized else ""
        )

    rows = conn.execute(
        f"""
        SELECT ce.unit_code, pc.criteria_text_raw AS evidence_text
               {evidence_projection('pc.criteria_text_raw')}
        FROM competency_elements ce
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND pc.criteria_text_raw IS NOT NULL
          AND {evidence_filter('pc.criteria_text_raw')}
        UNION ALL
        SELECT ce.unit_code, pc.criteria_text_refined AS evidence_text
               {evidence_projection('pc.criteria_text_refined')}
        FROM competency_elements ce
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND pc.criteria_text_refined IS NOT NULL
          AND {evidence_filter('pc.criteria_text_refined')}
        UNION ALL
        SELECT ce.unit_code, ki.ksa_text_raw AS evidence_text
               {evidence_projection('ki.ksa_text_raw')}
        FROM competency_elements ce
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND ki.ksa_text_raw IS NOT NULL
          AND {evidence_filter('ki.ksa_text_raw')}
        UNION ALL
        SELECT ce.unit_code, ki.ksa_text_refined AS evidence_text
               {evidence_projection('ki.ksa_text_refined')}
        FROM competency_elements ce
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND ki.ksa_text_refined IS NOT NULL
          AND {evidence_filter('ki.ksa_text_refined')}
        """,
        parameters,
    ).fetchall()
    evidence_by_unit: dict[str, list[str]] = {code: [] for code in candidates}
    for row in rows:
        evidence_by_unit.setdefault(str(row["unit_code"]), []).append(
            str(row["evidence_match_text" if normalized else "evidence_text"] or "")
        )
    weights = token_weights or {}
    scores: dict[str, float] = {}
    for code, evidence_rows in evidence_by_unit.items():
        score = 0.0
        matched_token_count = 0
        for token in tokens:
            if any(
                (
                    _ncs_search_boundary_match_normalized(
                        evidence, normalize_search_text(token)
                    ) if normalized else _ncs_search_boundary_match(evidence, token)
                ) == 1
                for evidence in evidence_rows
            ):
                matched_token_count += 1
                token_factor = weights.get(
                    token,
                    _NCS_SEARCH_GENERIC_TOKEN_FACTOR
                    if token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
                    else 1.0,
                )
                score += _NCS_SEARCH_TASK_KSA_WEIGHT * token_factor
        # One generic task/KSA word is too weak to overturn a lexical result;
        # require two independent query tokens before enabling the boost.
        scores[code] = score if matched_token_count >= 2 else 0.0
    return scores


def _ncs_search_unit_fallback_score(
    item: dict[str, Any],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    token_weights: dict[str, float] | None,
    compound_subphrase_expansions: dict[str, list[str]] | None = None,
    *,
    normalized: bool | str = False,
) -> float:
    """Reconstruct the lexical fallback score for stable second-stage sorting."""
    fields = item.get("_search_fields") or {}
    weighted_fields = (
        ("unit_name", 3.0),
        ("alias", 3.0),
        ("classification", 1.5),
        ("definition", _NCS_SEARCH_DEFINITION_WEIGHT),
    )
    expansions = token_expansions or {}
    weights = token_weights or {}
    score = 0.0
    for token in fallback_tokens:
        token_factor = weights.get(
            token,
            _NCS_SEARCH_GENERIC_TOKEN_FACTOR
            if token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
            else 1.0,
        )
        terms = [token, *expansions.get(token, [])]
        for field_name, field_weight in weighted_fields:
            field_value = fields.get(field_name)
            if any(
                (
                    _ncs_search_boundary_match_normalized(
                        normalize_search_text(field_value), normalize_search_text(term)
                    ) if normalized and field_name != "unit_code"
                    else _ncs_search_boundary_match(field_value, term)
                ) == 1
                for term in terms
            ):
                score += field_weight * token_factor
    # Keep the scoped spaced-compound signal consistent with the SQL tier
    # score.  Compound candidates are intentionally restricted to official
    # unit names and validated aliases; they must never contribute through a
    # classification label or definition during the Python re-rank pass.
    if compound_subphrase_expansions:
        seen_compound_terms: set[str] = set()
        for alternatives in compound_subphrase_expansions.values():
            for term in alternatives:
                if term in seen_compound_terms:
                    continue
                seen_compound_terms.add(term)
                if any(
                    (
                        _ncs_search_boundary_match_normalized(
                            normalize_search_text(fields.get(field_name)),
                            normalize_search_text(term),
                        )
                        if normalized
                        else _ncs_search_boundary_match(fields.get(field_name), term)
                    ) == 1
                    for field_name in ("unit_name", "alias")
                ):
                    score += 2.0
    return score


def _rerank_ncs_unit_task_ksa_candidates(
    candidates: list[dict[str, Any]],
    task_ksa_scores: dict[str, float],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    token_weights: dict[str, float] | None,
    compound_subphrase_expansions: dict[str, list[str]] | None = None,
    *,
    normalized: bool | str = False,
) -> list[dict[str, Any]]:
    """Apply supporting task/KSA evidence only within the OR fallback tier."""
    if not candidates or not task_ksa_scores:
        return candidates
    scored = []
    for index, item in enumerate(candidates):
        code = str(item.get("id") or "")
        lexical_score = _ncs_search_unit_fallback_score(
            item,
            fallback_tokens,
            token_expansions,
            token_weights,
            compound_subphrase_expansions,
            normalized=normalized,
        )
        scored.append(
            (
                lexical_score + task_ksa_scores.get(code, 0.0),
                -index,
                item,
            )
        )
    return [
        item
        for _, _, item in sorted(scored, key=lambda row: (-row[0], -row[1]))
    ]


def _ncs_search_unit_nongeneric_coverage(
    item: dict[str, Any],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    *,
    normalized: bool | str = False,
) -> float:
    """Fraction of non-generic query tokens evidenced on a retrieved unit."""
    nongeneric = [
        token
        for token in fallback_tokens
        if token and token.casefold() not in _NCS_SEARCH_GENERIC_TOKENS
    ]
    if not nongeneric:
        return 0.0
    fields = item.get("_search_fields") or {}
    expansions = token_expansions or {}
    matched = 0
    for token in nongeneric:
        terms = [token, *expansions.get(token, [])]
        if any(
            (
                _ncs_search_boundary_match_normalized(
                    normalize_search_text(field_value),
                    normalize_search_text(term),
                )
                if normalized and field_name != "unit_code"
                else _ncs_search_boundary_match(field_value, term)
            )
            == 1
            for field_name, field_value in fields.items()
            for term in terms
        ):
            matched += 1
    return matched / len(nongeneric)


def _rerank_ncs_unit_soft_scope_and_diversity(
    candidates: list[dict[str, Any]],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    token_weights: dict[str, float] | None,
    compound_subphrase_expansions: dict[str, list[str]] | None = None,
    *,
    normalized: bool | str = False,
    classification_filter: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Soft coverage prior + selective major diversity for bare token-OR units.

    Does not invent a hard classification filter. Diversity only demotes weak
    single-token matches that collapse one major; multi-token evidence keeps
    score order so same-major true positives are not displaced.
    """
    if not candidates or classification_filter:
        return candidates
    scored: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, item in enumerate(candidates):
        lexical_score = _ncs_search_unit_fallback_score(
            item,
            fallback_tokens,
            token_expansions,
            token_weights,
            compound_subphrase_expansions,
            normalized=normalized,
        )
        coverage = _ncs_search_unit_nongeneric_coverage(
            item,
            fallback_tokens,
            token_expansions,
            normalized=normalized,
        )
        # Coverage is a tie-break only. Adding it into the primary score buried
        # official compound names such as 해외법인설립 behind spaced variants.
        scored.append((lexical_score, coverage, index, item))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    selected: list[dict[str, Any]] = []
    for lexical_score, coverage, _index, item in scored:
        item["_soft_scope"] = {
            "coverage_tiebreak_applied": True,
            "coverage": round(coverage, 6),
            "lexical_score": round(lexical_score, 6),
            "prior_applied": False,
            "hard_filter_applied": False,
            "diversity_applied": False,
        }
        selected.append(item)
    return selected


def _normalized_ncs_search_params(params: dict[str, Any]) -> dict[str, Any]:
    """Keep original code binds while normalizing text binds once per tier."""
    result = dict(params)
    for key, value in params.items():
        if key in {"phrase_term", "joined_compound"} or key.startswith(
            ("token_", "expanded_", "intent_", "morphology_", "compound_base_")
        ):
            result[f"{key}_raw"] = value
            result[key] = normalize_search_text(value)
    # phrase_pattern is the legacy unit-order tiebreak, not a text prefilter.
    return result


def _ncs_search_tier_predicates(
    columns: tuple[str, ...],
    phrase: str,
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None = None,
    compound_subphrase_expansions: dict[str, list[str]] | None = None,
    weighted_columns: tuple[tuple[str, float], ...] | None = None,
    token_weights: dict[str, float] | None = None,
    *,
    normalized: bool | str = False,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_ncs_search_like(phrase)}%",
        "phrase_term": phrase,
    }
    phrase_clause = _ncs_search_boundary_any(
        columns, "phrase_term", normalized=normalized
    )
    joined_compound = _ncs_search_joined_compound_phrase(phrase, fallback_tokens)
    if joined_compound and "cu.unit_name_raw" in columns:
        params["joined_compound"] = joined_compound
        phrase_clause = (
            f"({phrase_clause} OR "
            f"{_ncs_search_boundary_any(('cu.unit_name_raw',), 'joined_compound', normalized=normalized)})"
        )
    token_clauses: list[str] = []
    parameter_groups: list[list[str]] = []
    for index, token in enumerate(fallback_tokens):
        parameter = f"token_{index}"
        params[parameter] = token
        token_clauses.append(_ncs_search_boundary_any(columns, parameter, normalized=normalized))
        parameter_groups.append([parameter])
    if not token_clauses:
        if normalized:
            params = _normalized_ncs_search_params(params)
        return [(0, phrase_clause, params, "", "")]
    token_and = "(" + " AND ".join(token_clauses) + ")"
    expansion_map = token_expansions or {}
    has_expansions = any(
        expansion_map.get(token) for token in fallback_tokens
    )
    if has_expansions:
        for token_index, token in enumerate(fallback_tokens):
            for alternative_index, alternative in enumerate(
                expansion_map.get(token, []),
                start=1,
            ):
                parameter = f"expanded_{token_index}_{alternative_index}"
                params[parameter] = alternative
                parameter_groups[token_index].append(parameter)
    search_groups = [
        "(" + " OR ".join(
            _ncs_search_boundary_any(columns, parameter, normalized=normalized)
            for parameter in group
        ) + ")"
        for group in parameter_groups
    ]
    compound_groups: list[str] = []
    compound_terms: list[str] = []
    if compound_subphrase_expansions and "cu.unit_name_raw" in columns:
        # Compound recovery is restricted to the official unit name and its
        # validated alias projection.  Classification labels and definitions
        # are intentionally excluded so a short joined phrase cannot promote
        # an unrelated hierarchy node.
        for token in fallback_tokens:
            for alternative in compound_subphrase_expansions.get(token, []):
                if alternative in compound_terms:
                    continue
                parameter = f"compound_base_{len(compound_terms)}"
                params[parameter] = alternative
                compound_terms.append(alternative)
                compound_groups.append(
                    _ncs_search_boundary_any(
                        ("cu.unit_name_raw", "aliases.alias_search_text"),
                        parameter,
                        normalized=normalized,
                    )
                )
    token_or = "(" + " OR ".join(search_groups) + ")"
    score_clause, meaningful_clause, rank_params = _ncs_search_fallback_ranking(
        weighted_columns or tuple((column, 1.0) for column in columns),
        fallback_tokens,
        parameter_groups,
        search_groups,
        token_weights,
        normalized=normalized,
    )
    params.update(rank_params)
    if compound_groups:
        # A joined subphrase is a positive unit-name signal, but weaker than a
        # complete phrase or token-AND tier.  Keep it in the same token-OR
        # tier so pagination and match-mode contracts remain unchanged.
        for index, clause in enumerate(compound_groups):
            weight_parameter = f"compound_rank_weight_{index}"
            params[weight_parameter] = 2.0
            score_clause += (
                f" + CASE WHEN {clause} THEN :{weight_parameter} ELSE 0 END"
            )
        compound_clause = "(" + " OR ".join(compound_groups) + ")"
        meaningful_clause = (
            compound_clause
            if meaningful_clause == "0 = 1"
            else f"({meaningful_clause} OR {compound_clause})"
        )
    if normalized:
        params = _normalized_ncs_search_params(params)
    # With one unchanged token the phrase already tests the same rows. If
    # it is empty, neither repeating it as AND nor scoring it as OR can help.
    single_token_phrase = len(fallback_tokens) == 1 and fallback_tokens[0] == phrase and not joined_compound
    tiers = [(0, phrase_clause, dict(params), "", "")]
    if not single_token_phrase:
        tiers.append((1, token_and, dict(params), "", ""))
    if has_expansions:
        tiers.append(
            (
                2,
                "(" + " AND ".join(search_groups) + ")",
                dict(params),
                score_clause,
                meaningful_clause if meaningful_clause == "0 = 1" else "",
            )
        )
    # After generic-only rows are excluded, the OR candidate set is exactly the
    # union of non-generic token groups.  Use that equivalent predicate directly
    # so SQLite does not repeat every generic LIKE check in WHERE.
    token_or_candidates = (
        meaningful_clause if meaningful_clause != "0 = 1" else token_or
    )
    if not single_token_phrase or has_expansions or compound_groups:
        tiers.append(
            (
                3,
                token_or_candidates,
                dict(params),
                score_clause,
                meaningful_clause if meaningful_clause == "0 = 1" else "",
            )
        )
    morphology = _ncs_search_morphology_expansions(fallback_tokens)
    if morphology:
        morphology_params: dict[str, Any] = {}
        morphology_groups = []
        morphology_tokens = []
        morphology_parameter_groups = []
        compound_score_terms = []
        field_weights = dict(weighted_columns or ())
        for index, token in enumerate(fallback_tokens):
            stem = morphology.get(token, [token])[0]
            parameter = f"morphology_{index}"
            morphology_params[parameter] = stem
            morphology_tokens.append(stem)
            morphology_parameter_groups.append([parameter])
            group = _ncs_search_boundary_any(columns, parameter, normalized=normalized)
            # A redundant workflow suffix may hide an official unit subject.
            # Expand only within the weakest unit tier, and only against the
            # unit name or an exact alias belonging to that same unit. Never
            # search the shorter base across definitions/classifications.
            if token in morphology and "cu.unit_name_raw" in columns:
                for base in _ncs_search_morphology_compound_bases(stem):
                    base_parameter = f"compound_base_{index}"
                    morphology_params[base_parameter] = base
                    if not normalized:
                        morphology_params[f"{base_parameter}_raw"] = base
                    name_match = _ncs_search_boundary_any(
                        ("cu.unit_name_raw",), base_parameter, normalized=normalized,
                    )
                    alias_match = f"""(aliases.alias_search_text IS NOT NULL AND EXISTS (
                        SELECT 1 FROM ncs_query_aliases compound_alias
                        WHERE compound_alias.unit_code = cu.unit_code AND (
                            compound_alias.alias_text COLLATE NOCASE = :{base_parameter}_raw
                            OR compound_alias.normalized_query COLLATE NOCASE = :{base_parameter}_raw
                        )
                    ))"""
                    group = f"({group} OR {name_match} OR {alias_match})"
                    for field, match, weight in (
                        ("cu.unit_name_raw", name_match, "name"),
                        ("aliases.alias_search_text", alias_match, "alias"),
                    ):
                        original_match = _ncs_search_boundary_any(
                            (field,), parameter, normalized=normalized,
                        )
                        weight_parameter = f"rank_compound_{weight}_{index}"
                        morphology_params[weight_parameter] = field_weights.get(field, 3.0)
                        # Credit a field at most once per query token, even if
                        # both the full stem and its compound base match it.
                        compound_score_terms.append(
                            f"CASE WHEN {match} AND NOT {original_match} "
                            f"THEN :{weight_parameter} ELSE 0 END"
                        )
            morphology_groups.append(group)
        morphology_score, morphology_meaningful, morphology_weights = (
            _ncs_search_fallback_ranking(
                weighted_columns or tuple((column, 1.0) for column in columns),
                morphology_tokens, morphology_parameter_groups, morphology_groups,
                normalized=normalized,
            )
        )
        morphology_params.update(morphology_weights)
        if compound_score_terms:
            morphology_score += " + " + " + ".join(compound_score_terms)
        if normalized:
            morphology_params = _normalized_ncs_search_params(morphology_params)
        morphology_params = {**params, **morphology_params}
        # All query terms must still match. Exclude original OR candidates so
        # the bounded fill query cannot spend its limit on existing rows.
        original_or = token_or_candidates
        if meaningful_clause == "0 = 1":
            original_or = "0 = 1"
        tiers.append((
            4,
            "(" + " AND ".join(morphology_groups) + f") AND NOT ({original_or})",
            morphology_params, morphology_score,
            morphology_meaningful if morphology_meaningful == "0 = 1" else "",
        ))
    return tiers


def _prepend_ncs_search_intent_tier(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    *,
    columns: tuple[str, ...],
    weighted_columns: tuple[tuple[str, float], ...],
    phrase: str,
    intent_expansions: list[str],
    normalized: bool | str = False,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    """Prepend a scored unit-only tier for high-confidence official terms."""
    if not intent_expansions:
        return tiers
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_ncs_search_like(phrase)}%",
    }
    parameter_groups: list[list[str]] = []
    search_groups: list[str] = []
    for index, alternative in enumerate(intent_expansions):
        parameter = f"intent_{index}"
        params[parameter] = alternative
        parameter_groups.append([parameter])
        search_groups.append(_ncs_search_boundary_any(columns, parameter, normalized=normalized))
    score_clause, _, rank_params = _ncs_search_fallback_ranking(
        weighted_columns,
        intent_expansions,
        parameter_groups,
        search_groups,
        normalized=normalized,
    )
    params.update(rank_params)
    if normalized:
        params = _normalized_ncs_search_params(params)
    intent_tier = (
        -1,
        "(" + " OR ".join(search_groups) + ")",
        params,
        score_clause,
        "",
    )
    return [intent_tier, *tiers]


def _execute_ncs_search_tiers(
    conn: Any,
    sql_template: str,
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    base_params: dict[str, Any],
) -> list[Any]:
    """Preserve lexical ranks; only append morphology to an underfilled OR tier."""
    original_rows: list[Any] = []
    for match_tier, where_clause, tier_params, score_clause, meaningful_clause in tiers:
        params = dict(tier_params)
        params.update(base_params)
        params["match_tier"] = match_tier
        if match_tier == 3:
            params["candidate_limit"] = max(
                base_params["candidate_limit"],
                base_params.get("rerank_candidate_limit", 0),
            )
        if original_rows:
            params["candidate_limit"] -= len(original_rows)
        rows = conn.execute(
            sql_template.format(
                where_clause=where_clause,
                fallback_filter_clause=(
                    f" AND ({meaningful_clause})" if meaningful_clause else ""
                ),
                fallback_order_clause=(
                    f"({score_clause}) DESC," if score_clause else ""
                ),
            ),
            params,
        ).fetchall()
        if rows:
            if match_tier == 3 and len(rows) < base_params["candidate_limit"]:
                original_rows = rows
                continue
            return original_rows + rows
    return original_rows


_KSA_FTS_BIND_PATTERN = re.compile(
    r"LIKE '%' \|\| :([A-Za-z0-9_]+) \|\| '%'"
)
_KSA_FTS_SAFE_TRIGRAM = re.compile(r"[A-Za-z0-9\uac00-\ud7a3]{3}")


def _compact_ksa_search_fts_available(conn: Any, normalized: bool | str) -> bool:
    """Use only a manifest-attested v2 compact index."""
    if normalized != "v2":
        return False
    row = conn.execute(
        "SELECT manifest_value FROM serving_snapshot_manifest "
        "WHERE manifest_key = 'ksa_search_fts_schema'"
    ).fetchone()
    if not row or row[0] != "ncs_ksa_search_fts_v1":
        return False
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ksa_search_fts'"
    ).fetchone() is not None


def _compact_fts_tiers(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    *,
    index_table: str,
    identifier: str,
    parameter: str,
    anchor_for: Any,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    """Apply necessary candidate conditions without changing the search predicate.

    Tier 1 requires every token. Tier 2 requires every token's OR group of
    alternatives. A group with an unindexable alternative cannot restrict FTS,
    but other mandatory groups can. OR tiers require every alternative to be
    indexable. SQL identifiers are supplied only by the wrappers below.
    """
    filtered = []
    for match_tier, where_clause, params, score_clause, meaningful_clause in tiers:
        bind_names = set(_KSA_FTS_BIND_PATTERN.findall(where_clause))
        anchors = {name: anchor_for(params.get(name)) for name in bind_names}
        fts_match = ""
        if bind_names and len(bind_names) <= 32:
            groups: dict[str, list[str]] = {}
            if match_tier in (1, 2):
                for name in sorted(bind_names):
                    token = re.fullmatch(r"token_(\d+)", name)
                    expanded = (
                        re.fullmatch(r"expanded_(\d+)_\d+", name)
                        if match_tier == 2 else None
                    )
                    match = token or expanded
                    if match is None:
                        groups = {}
                        break
                    groups.setdefault(match[1], []).append(name)
                if any(f"token_{index}" not in names for index, names in groups.items()):
                    groups = {}
            if groups:
                usable = []
                for names in groups.values():
                    if all(anchors[name] for name in names):
                        alternatives = sorted({anchors[name] for name in names})
                        usable.append("(" + " OR ".join('"' + value + '"' for value in alternatives) + ")")
                fts_match = " AND ".join(usable)
            elif all(anchors.values()):
                fts_match = " OR ".join('"' + value + '"' for value in sorted(set(anchors.values())))
        if not fts_match:
            filtered.append((match_tier, where_clause, params, score_clause, meaningful_clause))
            continue
        filtered.append((
            match_tier,
            f"{identifier} IN (SELECT rowid FROM {index_table} "
            f"WHERE {index_table} MATCH :{parameter}) AND (" + where_clause + ")",
            {**params, parameter: fts_match},
            score_clause, meaningful_clause,
        ))
    return filtered


def _compact_ksa_search_fts_tiers(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    """Keep older compact snapshots' trigram candidate index usable."""
    def trigram(value: Any) -> str | None:
        match = _KSA_FTS_SAFE_TRIGRAM.search(str(value or ""))
        return match.group() if match else None

    return _compact_fts_tiers(
        tiers, index_table="ksa_search_fts", identifier="ki.ksa_id",
        parameter="_ksa_fts_match", anchor_for=trigram,
    )


def _compact_lexical_prefix_available(conn: Any, normalized: bool | str) -> bool:
    """Only use a complete, Builder-attested pair of normalized prefix indexes."""
    if normalized != "v2":
        return False
    keys = tuple(PREFIX_FTS_REQUIRED_MANIFEST)
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        "SELECT manifest_key, manifest_value FROM serving_snapshot_manifest "
        f"WHERE manifest_key IN ({placeholders})", keys,
    ).fetchall()
    if len(rows) != len(keys) or dict(rows) != PREFIX_FTS_REQUIRED_MANIFEST:
        return False
    names = set(PREFIX_FTS_TABLES.values())
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        tuple(sorted(names)),
    ).fetchall()
    return {row[0] for row in tables} == names


def _compact_lexical_prefix_tiers(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    scope: str,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    return _compact_fts_tiers(
        tiers, index_table=PREFIX_FTS_TABLES[scope],
        identifier={"ksa": "ki.ksa_id", "criteria": "pc.criteria_id"}[scope],
        parameter="_lexical_prefix_match", anchor_for=prefix_fts_term,
    )


def _ncs_search_match_metadata(
    item: dict[str, Any],
    *,
    query_tokens: list[str],
    phrase: str,
    match_mode: str,
    token_expansions: dict[str, list[str]] | None = None,
    compound_subphrase_expansions: dict[str, list[str]] | None = None,
    intent_expansions: list[str] | None = None,
    normalized: bool | str = False,
) -> None:
    raw_fields = item.pop("_search_fields", {})
    normalized_fields = {
        field_name: (
            normalize_search_text(field_value)
            if normalized and field_name != "unit_code"
            else _normalize_ncs_search_text(field_value).casefold()
        )
        for field_name, field_value in raw_fields.items()
        if field_value is not None
    }

    def matches(field_name: str, value: str, term: str) -> int:
        if normalized and field_name != "unit_code":
            return _ncs_search_boundary_match_normalized(value, normalize_search_text(term))
        return _ncs_search_boundary_match(value, term)

    active_expansions = (
        token_expansions or {}
        if match_mode in {"expanded_token_and", "token_or"}
        else {}
    )
    active_compound_expansions = (
        compound_subphrase_expansions or {} if match_mode == "token_or" else {}
    )
    if match_mode == "morphology_fill":
        active_expansions = _ncs_search_morphology_expansions(query_tokens)
        if item.get("type") == "unit":
            active_expansions = {
                token: [*stems, *_ncs_search_morphology_compound_bases(stems[0])]
                for token, stems in active_expansions.items()
            }
    matched_tokens: list[str] = []
    matched_expansions: list[dict[str, Any]] = []
    matched_terms: list[str] = []
    matched_term_fields: dict[str, set[str]] = {}
    if match_mode == "intent_alias":
        for expansion in intent_expansions or []:
            normalized_expansion = expansion.casefold()
            expansion_fields = [
                field_name
                for field_name, value in normalized_fields.items()
                if normalized_expansion
                and matches(field_name, value, normalized_expansion)
            ]
            if not expansion_fields:
                continue
            matched_terms.append(normalized_expansion)
            matched_term_fields.setdefault(normalized_expansion, set()).update(expansion_fields)
            matched_expansions.append(
                {
                    "query": phrase,
                    "matched_as": expansion,
                    "match_fields": expansion_fields,
                }
            )
    for token in query_tokens:
        normalized_token = token.casefold()
        direct_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if normalized_token and matches(field_name, value, normalized_token)
        ]
        if direct_fields:
            matched_tokens.append(token)
            matched_terms.append(normalized_token)
            matched_term_fields.setdefault(normalized_token, set()).update(direct_fields)
            # A direct definition/classification hit can coexist with a
            # stronger official compound hit in the unit name. Keep the
            # compound evidence visible instead of hiding it behind the
            # direct-token fast path.
            if item.get("type") == "unit":
                for expansion in active_compound_expansions.get(token, []):
                    normalized_expansion = expansion.casefold()
                    expansion_fields = [
                        field_name
                        for field_name, value in normalized_fields.items()
                        if field_name in {"unit_name", "alias"}
                        and normalized_expansion
                        and matches(field_name, value, normalized_expansion)
                    ]
                    if not expansion_fields or any(
                        entry.get("token") == token
                        and entry.get("matched_as") == expansion
                        for entry in matched_expansions
                    ):
                        continue
                    matched_terms.append(normalized_expansion)
                    matched_term_fields.setdefault(normalized_expansion, set()).update(
                        expansion_fields
                    )
                    matched_expansions.append(
                        {
                            "token": token,
                            "matched_as": expansion,
                            "match_fields": expansion_fields,
                        }
                    )
            continue
        alternatives = list(active_expansions.get(token, []))
        if item.get("type") == "unit" and match_mode == "token_or":
            alternatives.extend(
                term for term in active_compound_expansions.get(token, [])
                if term not in alternatives
            )
        for expansion in alternatives:
            normalized_expansion = expansion.casefold()
            compound_base = (
                match_mode == "morphology_fill"
                and item.get("type") == "unit"
                and expansion != active_expansions[token][0]
            )
            name_only = compound_base or (
                item.get("type") == "unit"
                and expansion in active_compound_expansions.get(token, [])
                # A term can independently have general expansion provenance.
                # Keep its legitimate definition/classification evidence then.
                and expansion not in active_expansions.get(token, [])
            )
            expansion_fields = [
                field_name
                for field_name, value in normalized_fields.items()
                if normalized_expansion
                and (not name_only or field_name in {"unit_name", "alias"})
                and matches(field_name, value, normalized_expansion)
            ]
            if not expansion_fields:
                continue
            matched_tokens.append(token)
            matched_terms.append(normalized_expansion)
            matched_term_fields.setdefault(normalized_expansion, set()).update(expansion_fields)
            matched_expansions.append(
                {
                    "token": token,
                    "matched_as": expansion,
                    "match_fields": expansion_fields,
                }
            )
            break
    normalized_phrase = phrase.casefold()
    if match_mode == "phrase":
        match_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if normalized_phrase and matches(field_name, value, normalized_phrase)
        ]
        if not match_fields and item.get("type") == "unit":
            joined_compound = _ncs_search_joined_compound_phrase(
                phrase,
                [token for token in query_tokens if len(token) > 1],
            )
            compound_fields = [
                field_name
                for field_name, value in normalized_fields.items()
                if field_name == "unit_name"
                and joined_compound
                and matches(field_name, value, joined_compound)
            ]
            if compound_fields:
                matched_tokens = list(query_tokens)
                match_fields = compound_fields
                matched_expansions.append(
                    {
                        "query": phrase,
                        "matched_as": joined_compound,
                        "match_fields": compound_fields,
                    }
                )
    else:
        match_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if any(field_name in matched_term_fields.get(term, set()) for term in matched_terms)
        ]
    item.pop("_match_tier", None)
    item["match_mode"] = match_mode
    item["matched_tokens"] = matched_tokens
    item["match_fields"] = match_fields
    item["matched_expansions"] = matched_expansions


def _round_robin_ncs_search_results(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    requested_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while True:
        appended = False
        for item_type in requested_types:
            candidates = candidates_by_type.get(item_type, [])
            if index < len(candidates):
                merged.append(candidates[index])
                appended = True
        if not appended:
            return merged
        index += 1


def search_ncs(
    query: str,
    scope: str = "all",
    limit: int = 50,
    offset: int = 0,
    classification_filter: dict[str, Any] | None = None,
    context_text: str | None = None,
    job_scope: str | None = None,
) -> dict[str, Any]:
    """Search NCS evidence with phrase, token-AND, and token-OR fallback."""
    max_rows = _required_runtime_helper("clamp_limit", _CLAMP_LIMIT)(limit)
    try:
        applied_offset = min(max(int(offset), 0), 10_000)
    except (TypeError, ValueError):
        applied_offset = 0
    normalized_scope = scope if scope in _NCS_SEARCH_TYPES or scope == "all" else "all"
    requested_types = (
        _NCS_SEARCH_TYPES if normalized_scope == "all" else (normalized_scope,)
    )
    phrase, query_tokens, fallback_tokens = _normalize_ncs_search_query(query)
    joined_compound = _ncs_search_joined_compound_phrase(phrase, fallback_tokens)
    normalized_classification_filter = _normalize_ncs_classification_filter(
        classification_filter
    )
    # Validate independently supplied context before touching the DB.  The
    # normalized free text is never included in the response.
    normalized_context_text, normalized_job_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    # Practitioner hints inspect the full query, including words outside the
    # bounded fallback tokens.
    intent_expansions = _ncs_search_intent_expansions(
        _normalize_ncs_search_text(query)
    )
    empty_counts = {item_type: 0 for item_type in requested_types}
    empty_more = {item_type: False for item_type in requested_types}
    if not phrase:
        with _required_runtime_helper("open_db", _OPEN_DB_FACTORY)() as conn:
            search_context = resolve_ncs_search_context(
                conn,
                context_text=context_text,
                job_scope=job_scope,
                classification_filter=classification_filter,
            )
        return {
            "query": query,
            "normalized_query": phrase,
            "query_tokens": query_tokens,
            "scope": normalized_scope,
            "classification_filter": normalized_classification_filter,
            "classification_filter_applied": bool(normalized_classification_filter),
            "match_mode": None,
            "query_expansions": {},
            "query_intent_expansions": [],
            "counts_by_type": empty_counts,
            "has_more_by_type": empty_more,
            "returned": 0,
            "offset": applied_offset,
            "next_offset": None,
            "search_context": search_context,
            "markdown_summary": _ncs_search_markdown(
                query,
                [],
                counts_by_type=empty_counts,
                offset=applied_offset,
                next_offset=None,
            ),
            "results": [],
        }

    candidate_limit = applied_offset + max_rows + 1
    unit_candidate_limit = max(candidate_limit, _NCS_SEARCH_UNIT_RERANK_WINDOW + 1)
    raw_candidates: dict[str, list[dict[str, Any]]] = {
        item_type: [] for item_type in requested_types
    }
    unit_task_ksa_scores: dict[str, float] = {}
    search_context: dict[str, Any]
    with _required_runtime_helper("open_db", _OPEN_DB_FACTORY)() as conn:
        _register_ncs_search_udfs(conn)
        search_context = resolve_ncs_search_context(
            conn,
            context_text=context_text,
            job_scope=job_scope,
            classification_filter=classification_filter,
        )
        normalized_search = _normalized_search_storage(conn)
        if (
            "unit" in requested_types
            and intent_expansions
            and _ncs_search_has_exact_unit_name(
                conn, phrase, normalized_classification_filter,
                normalized=normalized_search,
            )
        ):
            intent_expansions = []
        lexical_prefix_available = (
            _compact_lexical_prefix_available(conn, normalized_search)
            if any(kind in requested_types for kind in ("criteria", "ksa")) else False
        )
        tier_options = {"normalized": normalized_search} if normalized_search else {}
        token_expansions = _active_token_expander()(
            conn,
            fallback_tokens,
        )
        # Unit ranking uses words resolved against the unit corpus: long
        # sentences keep their specific terms instead of the first four raw
        # words, and absent closed compounds fall back to their head noun.
        # Element, criterion, and KSA search keep the original tokens.
        unit_terms = list(fallback_tokens)
        unit_term_trace: dict[str, str] = {}
        unit_evidence_words: list[str] = []
        if "unit" in requested_types:
            selected_terms, unit_term_trace, unit_evidence_words = _select_ncs_search_unit_terms(
                conn,
                phrase,
                fallback_tokens,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            if selected_terms:
                unit_terms = selected_terms
        unit_base_expansions = (
            token_expansions
            if unit_terms == fallback_tokens
            else _active_token_expander()(conn, unit_terms)
        )
        # Compound subphrase recovery is activated only inside an explicit
        # source-backed classification scope.  Outside a hard scope, the same
        # joined token can be a valid term in another NCS major and changing
        # its rank would trade away precision for broad lexical recall.
        unit_compound_expansions = (
            _ncs_search_joined_compound_subphrases(unit_terms)
            if normalized_classification_filter
            else {}
        )
        # The union is for response metadata only. Joined subphrases have their
        # own name/alias-only OR predicate and must not enter the general token
        # groups, which also search definitions and classification labels.
        unit_token_expansions = {
            token: list(alternatives)
            for token, alternatives in unit_base_expansions.items()
        }
        for token, alternatives in unit_compound_expansions.items():
            values = unit_token_expansions.setdefault(token, [])
            for alternative in alternatives:
                if alternative not in values:
                    values.append(alternative)
        leaf_token_expansions = _ncs_search_leaf_token_expansions(
            fallback_tokens,
            token_expansions,
        )
        # Only unit ranking consumes these corpus frequencies. Leaf-only
        # searches use their own fixed weights and need no unit-table scan.
        token_weights = (
            _ncs_search_token_idf_weights(
                conn,
                unit_terms,
                normalized_classification_filter,
                normalized=normalized_search,
            ) if "unit" in requested_types else {}
        )
        if "unit" in requested_types:
            columns = (
                "cu.unit_code",
                "cu.unit_name_raw",
                "cu.api_definition",
                "c.major_name",
                "c.middle_name",
                "c.small_name",
                "c.sub_name",
                "aliases.alias_search_text",
            )
            weighted_columns = (
                ("cu.unit_name_raw", 3.0),
                ("aliases.alias_search_text", 3.0),
                ("c.sub_name", 2.0),
                ("c.small_name", 2.0),
                ("c.middle_name", 1.0),
                ("c.major_name", 1.0),
                ("cu.api_definition", _NCS_SEARCH_DEFINITION_WEIGHT),
            )
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                unit_terms,
                unit_base_expansions,
                compound_subphrase_expansions=unit_compound_expansions,
                weighted_columns=weighted_columns,
                token_weights=token_weights,
                **tier_options,
            )
            tiers = _prepend_ncs_search_intent_tier(
                tiers,
                columns=columns,
                weighted_columns=weighted_columns,
                phrase=phrase,
                intent_expansions=intent_expansions,
                normalized=normalized_search,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            unit_order_name = _ncs_search_column(
                "cu.unit_name_raw", normalized_search
            )
            unit_order_definition = _ncs_search_column(
                "cu.api_definition", normalized_search
            )
            unit_order_classification = tuple(
                _ncs_search_column(f"c.{field}", normalized_search)
                for field in ("major_name", "middle_name", "small_name", "sub_name")
            )
            order_phrase = (
                normalize_search_text(phrase) if normalized_search else phrase
            )
            order_joined_compound = (
                normalize_search_text(joined_compound)
                if normalized_search
                else joined_compound
            )
            rows = _active_tier_executor()(
                conn,
                """
                WITH alias_search AS (
                    SELECT unit_code,
                           GROUP_CONCAT(
                               COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, ''),
                               ' '
                           ) AS alias_search_text
                """ + (
                    ", GROUP_CONCAT(alias_search_norm, ' ') AS alias_search_norm"
                    if normalized_search else ""
                ) + f"""
                    FROM ncs_query_aliases
                    WHERE unit_code IS NOT NULL
                    GROUP BY unit_code
                )
                SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition,
                       cu.unit_level_raw,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name,
                       c.duty_order, aliases.alias_search_text,
                       :match_tier AS match_tier
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
                WHERE {{where_clause}}{{fallback_filter_clause}}
                ORDER BY match_tier,
                    {{fallback_order_clause}}
                    CASE
                        WHEN cu.unit_code = :exact_code THEN 0
                        WHEN TRIM({unit_order_name}) = TRIM(:order_exact) COLLATE NOCASE THEN 0
                        WHEN {unit_order_name} LIKE :order_prefix_pattern ESCAPE '\\' THEN 1
                        WHEN :order_joined_exact != ''
                         AND TRIM({unit_order_name}) = TRIM(:order_joined_exact) COLLATE NOCASE THEN 2
                        WHEN :order_joined_exact != ''
                         AND {unit_order_name} LIKE :order_joined_prefix_pattern ESCAPE '\\' THEN 3
                        WHEN {unit_order_name} LIKE :order_phrase_pattern ESCAPE '\\' THEN 4
                        WHEN {unit_order_classification[0]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[1]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[2]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[3]} LIKE :order_phrase_pattern ESCAPE '\\' THEN 5
                        WHEN {unit_order_definition} LIKE :order_phrase_pattern ESCAPE '\\' THEN 6
                        ELSE 7
                    END,
                    LENGTH(cu.unit_name_raw),
                    CASE
                        WHEN SUBSTR(cu.unit_code, 1, 8) =
                             COALESCE(c.major_code, '')
                             || COALESCE(c.middle_code, '')
                             || COALESCE(c.small_code, '')
                             || COALESCE(c.sub_code, '')
                        THEN 0
                        ELSE 1
                    END,
                    cu.unit_code
                LIMIT :candidate_limit
                """,
                tiers,
                {
                    "exact_code": phrase,
                    "order_exact": order_phrase,
                    "order_prefix_pattern": f"{_escape_ncs_search_like(order_phrase)}%",
                    "order_joined_exact": order_joined_compound,
                    "order_joined_prefix_pattern": (
                        f"{_escape_ncs_search_like(order_joined_compound)}%"
                    ),
                    "order_phrase_pattern": f"%{_escape_ncs_search_like(order_phrase)}%",
                    "candidate_limit": candidate_limit,
                    "rerank_candidate_limit": unit_candidate_limit,
                },
            )
            for row in rows:
                raw_candidates["unit"].append(
                    {
                        "type": "unit",
                        "id": row["unit_code"],
                        "text": row["unit_name_raw"],
                        "unit_level": row["unit_level_raw"],
                        "path": _required_runtime_helper("unit_path", _UNIT_PATH)(row),
                        "api_definition": row["api_definition"],
                        "_match_tier": int(row["match_tier"]),
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
                        "_search_fields": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "definition": row["api_definition"],
                            "classification": " ".join(
                                str(row[key] or "")
                                for key in ("major_name", "middle_name", "small_name", "sub_name")
                            ),
                            "alias": row["alias_search_text"],
                        },
                    }
                )
            selected_unit_tier = min(
                (item["_match_tier"] for item in raw_candidates["unit"]),
                default=None,
            )
            if selected_unit_tier == 3:
                unit_task_ksa_scores = _ncs_search_unit_task_ksa_scores(
                    conn,
                    [item["id"] for item in raw_candidates["unit"] if item["_match_tier"] == 3][:_NCS_SEARCH_UNIT_RERANK_WINDOW],
                    [*unit_terms, *unit_evidence_words],
                    token_weights,
                    normalized=normalized_search,
                )

        if "element" in requested_types:
            columns = ("ce.element_name_raw",)
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                leaf_token_expansions,
                weighted_columns=(("ce.element_name_raw", 3.0),),
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name, c.duty_order,
                       :match_tier AS match_tier
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause}
                         LENGTH(ce.element_name_raw), ce.element_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["element"].append(
                    {
                        "type": "element",
                        "id": row["element_id"],
                        "text": row["element_name_raw"],
                        "path": _ncs_search_leaf_path(row),
                        "_match_tier": int(row["match_tier"]),
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
                        "_search_fields": {"element_name": row["element_name_raw"]},
                    }
                )

        if "criteria" in requested_types:
            columns = ("pc.criteria_text_raw", "pc.criteria_text_refined")
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                leaf_token_expansions,
                weighted_columns=(
                    ("pc.criteria_text_raw", 3.0),
                    ("pc.criteria_text_refined", 3.0),
                ),
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            if lexical_prefix_available:
                tiers = _compact_lexical_prefix_tiers(tiers, "criteria")
            rows = _active_tier_executor()(
                conn,
                """
                SELECT pc.criteria_id, pc.criteria_text_raw, pc.criteria_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name, c.duty_order,
                       :match_tier AS match_tier
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause} pc.criteria_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["criteria"].append(
                    {
                        "type": "criteria",
                        "id": row["criteria_id"],
                        "text": row["criteria_text_raw"],
                        "path": _ncs_search_leaf_path(row, include_element=True),
                        "_match_tier": int(row["match_tier"]),
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
                        "_search_fields": {
                            "criteria_text": row["criteria_text_raw"],
                            "criteria_text_refined": row["criteria_text_refined"],
                        },
                    }
                )

        if "ksa" in requested_types:
            columns = ("ki.ksa_text_raw", "ki.ksa_text_refined")
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                leaf_token_expansions,
                weighted_columns=(
                    ("ki.ksa_text_raw", 3.0),
                    ("ki.ksa_text_refined", 3.0),
                ),
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            if lexical_prefix_available:
                tiers = _compact_lexical_prefix_tiers(tiers, "ksa")
            elif _compact_ksa_search_fts_available(conn, normalized_search):
                tiers = _compact_ksa_search_fts_tiers(tiers)
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.ksa_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name, c.duty_order,
                       :match_tier AS match_tier
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause} ki.ksa_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["ksa"].append(
                    {
                        "type": "ksa",
                        "id": row["ksa_id"],
                        "text": row["ksa_text_raw"],
                        "ksa_type": row["ksa_type_name"],
                        "path": _ncs_search_leaf_path(row, include_element=True),
                        "_match_tier": int(row["match_tier"]),
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
                        "_search_fields": {
                            "ksa_text": row["ksa_text_raw"],
                            "ksa_text_refined": row["ksa_text_refined"],
                        },
                    }
                )

    selected_tier_by_type = {
        item_type: min(
            (int(item["_match_tier"]) for item in raw_candidates[item_type]),
            default=None,
        )
        for item_type in requested_types
    }
    modes_by_type = {
        item_type: {_NCS_SEARCH_MATCH_MODES[item["_match_tier"]] for item in rows}
        for item_type, rows in raw_candidates.items()
    }
    match_mode_by_type = {
        item_type: next(iter(modes)) if len(modes) == 1 else "mixed" if modes else None
        for item_type, modes in modes_by_type.items()
    }
    active_match_modes = {
        mode for modes in modes_by_type.values() for mode in modes
    }
    # Both fallback tiers consume expansions. Report the effective per-scope
    # maps, including scoped compounds only for token OR, rather than the input map:
    # leaf search deliberately disables job-scope reductions. This describes
    # retrieval alternatives; matched_expansions remains each row's evidence.
    applied_token_expansions: dict[str, list[str]] = {}
    for item_type, modes in modes_by_type.items():
        if not modes.intersection({"expanded_token_and", "token_or"}):
            continue
        if item_type == "unit":
            effective_expansions = (
                unit_token_expansions if "token_or" in modes else unit_base_expansions
            )
        else:
            effective_expansions = leaf_token_expansions
        for token, alternatives in effective_expansions.items():
            applied = applied_token_expansions.setdefault(token, [])
            for alternative in alternatives:
                if alternative not in applied:
                    applied.append(alternative)
    applied_intent_expansions = (
        intent_expansions
        if "intent_alias" in active_match_modes
        else []
    )
    match_mode = (
        next(iter(active_match_modes))
        if len(active_match_modes) == 1
        else "mixed" if active_match_modes else None
    )
    candidates_by_type = {item_type: list(rows) for item_type, rows in raw_candidates.items()}
    if selected_tier_by_type.get("unit") == 3 and unit_task_ksa_scores:
        unit_or_candidates = [
            item for item in candidates_by_type["unit"] if item["_match_tier"] == 3
        ]
        candidates_by_type["unit"] = _rerank_ncs_unit_task_ksa_candidates(
            unit_or_candidates[:_NCS_SEARCH_UNIT_RERANK_WINDOW],
            unit_task_ksa_scores,
            unit_terms,
            unit_base_expansions,
            token_weights,
            compound_subphrase_expansions=unit_compound_expansions,
            normalized=normalized_search,
        ) + unit_or_candidates[_NCS_SEARCH_UNIT_RERANK_WINDOW:] + [
            item for item in candidates_by_type["unit"] if item["_match_tier"] == 4
        ]
    if search_context.get("status") == "not_provided":
        search_context["needs_context"] = _ncs_search_needs_context(
            candidates_by_type,
            selected_tier_by_type,
        )
    if normalized_context_text or normalized_job_scope:
        _annotate_ncs_search_shadow(
            candidates_by_type,
            requested_types,
            search_context,
        )
    classification_scope_invariant = _ncs_search_scope_invariant(
        candidates_by_type,
        normalized_classification_filter,
        normalized=normalized_search,
    )
    # Cross-type balancing must not let new fill rows displace original results
    # from another scope. Keep the entire original round-robin prefix intact.
    if classification_scope_invariant and not classification_scope_invariant["ok"]:
        # A scope mismatch is a containment failure, not a weak match.  Do not
        # expose even a page that happened not to contain the offending row.
        merged = []
    else:
        merged = _round_robin_ncs_search_results(
            {kind: [item for item in rows if item["_match_tier"] != 4]
             for kind, rows in candidates_by_type.items()}, requested_types,
        ) + _round_robin_ncs_search_results(
            {kind: [item for item in rows if item["_match_tier"] == 4]
             for kind, rows in candidates_by_type.items()}, requested_types,
        )
    page_end = applied_offset + max_rows
    page = merged[applied_offset:page_end]
    # The rescue reorders what the caller actually sees, so "rank 4+" means the
    # same thing here as in the evaluation. Only unit rows move; every other
    # row keeps its position.
    semantic_rescue_evidence: dict[str, Any] | None = None
    if _SEMANTIC_PROVIDER is not None:
        unit_positions = [
            position for position, item in enumerate(page) if item["type"] == "unit"
        ]
        if len(unit_positions) > 3:
            reordered, semantic_rescue_evidence = rescue_order(
                [page[position] for position in unit_positions],
                query=phrase or query,
                provider=_SEMANTIC_PROVIDER,
                margin=_SEMANTIC_MARGIN,
            )
            for position, item in zip(unit_positions, reordered):
                page[position] = item
    consumed_by_type = {item_type: 0 for item_type in requested_types}
    for item in merged[:page_end]:
        consumed_by_type[item["type"]] += 1
    has_more_by_type: dict[str, bool] = {}
    for item_type in requested_types:
        selected_candidates = candidates_by_type[item_type]
        fetched = raw_candidates[item_type]
        selected_tier = selected_tier_by_type[item_type]
        may_have_more_selected = bool(
            selected_tier is not None
            and len(fetched) == (
                unit_candidate_limit if item_type == "unit" and selected_tier == 3
                else candidate_limit
            )
            and fetched
        )
        has_more_by_type[item_type] = (
            len(selected_candidates) > consumed_by_type[item_type]
            or may_have_more_selected
        )
    if classification_scope_invariant and not classification_scope_invariant["ok"]:
        has_more_by_type = dict(empty_more)
    counts_by_type = {item_type: 0 for item_type in requested_types}
    for item in page:
        counts_by_type[item["type"]] += 1
        item.pop("_classification_codes", None)
        item_token_expansions = (
            unit_base_expansions
            if item["type"] == "unit"
            else leaf_token_expansions
        )
        _ncs_search_match_metadata(
            item,
            query_tokens=unit_terms if item["type"] == "unit" else query_tokens,
            phrase=phrase,
            match_mode=_NCS_SEARCH_MATCH_MODES[item["_match_tier"]],
            token_expansions=item_token_expansions,
            compound_subphrase_expansions=(
                unit_compound_expansions if item["type"] == "unit" else None
            ),
            intent_expansions=intent_expansions,
            normalized=normalized_search,
        )
    next_offset = page_end if page and any(has_more_by_type.values()) else None
    result = {
        "query": query,
        "normalized_query": phrase,
        "query_tokens": query_tokens,
        "scope": normalized_scope,
        "classification_filter": normalized_classification_filter,
        "classification_filter_applied": bool(normalized_classification_filter),
        **(
            {"classification_scope_invariant": classification_scope_invariant}
            if classification_scope_invariant
            and not classification_scope_invariant["ok"] else {}
        ),
        "match_mode": match_mode,
        "match_mode_by_type": match_mode_by_type,
        "query_expansions": applied_token_expansions,
        "query_intent_expansions": applied_intent_expansions,
        **(
            {"unit_query_terms": {"terms": unit_terms, "resolved_from": unit_term_trace}}
            if unit_term_trace else {}
        ),
        "counts_by_type": counts_by_type,
        "has_more_by_type": has_more_by_type,
        "returned": len(page),
        "offset": applied_offset,
        "next_offset": next_offset,
        "search_context": search_context,
        **(
            {"semantic_rescue": semantic_rescue_evidence}
            if semantic_rescue_evidence else {}
        ),
        "results": page,
    }
    result["markdown_summary"] = _ncs_search_markdown(
        query,
        page,
        counts_by_type=counts_by_type,
        offset=applied_offset,
        next_offset=next_offset,
    )
    return result
