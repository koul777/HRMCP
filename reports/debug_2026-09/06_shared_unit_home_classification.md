# 6단계 — 공유 능력단위의 본가 분류 우선 정렬

작성일: 2026-09-11

## 발견 경위

5단계에서 배포한 intent alias를 프로덕션(`https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`)에서
재검증하던 중 `사옥 보안 점검` 결과에서 발견했다. 4단계 인수인계에 남아 있던 원격 확인 과제를
MCP 프로토콜로 직접 질의해 수행했고, Windows 콘솔 인코딩 문제 없이 한국어 질의가 전달됐다.

프로덕션 응답은 다음과 같았다.

| 순위 | 능력단위명 | 분류경로 | 능력단위코드 |
|---:|---|---|---|
| 1 | 총무보안관리 | 사회복지·종교 > 사회복지 > 사회복지서비스 > 자원봉사관리 | `0202010110_19v2` |
| 2 | 총무보안관리 | 경영·회계·사무 > 총무·인사 > 총무 > 총무 | `0202010110_25v3` |

## 원인 분석

### 데이터는 정상이다

처음에는 `classification_id`가 잘못 배정된 데이터 결함으로 판단했으나, 확인 결과 이는 NCS의
정상적인 **공유 능력단위** 구조다. 자원봉사관리(classification_id 114) 구성은 다음과 같다.

- native 11개: `0701020501`~`0701020511_24v1`, 연속·동일 버전
- 차용 3개: 예산 관리 `_13v1`, 총무보안관리 `_19v2`, 사무행정 업무 관리 `_22v4`

자원봉사 직무가 예산·보안·사무행정을 수행하므로 타당한 차용이며, 버전 태그가 제각각인 것은
차용 시점의 버전을 유지한 결과다. 전체 13,435행 중 코드 접두와 분류 코드가 어긋나는 행은
4개뿐이고, 그중 3개가 이 차용 구조다. 원천 DB(`data/processed/ncs.db`)에도 동일하게 존재해
서빙 DB 빌드가 만든 값이 아니다.

### 실제 결함은 정렬이다

능력단위 질의의 `ORDER BY` 마지막 기준이 `cu.unit_code`였다. 이름·tier·길이가 모두 같은
차용본과 본가가 만나면 문자열 정렬만 남는다.

```
0202010110_19v2   ← 차용본(자원봉사관리), "1" < "2" 이므로 먼저
0202010110_25v3   ← 본가(총무), 최신본
```

오래된 차용본이 1위가 되어, HR 담당자가 `사옥 보안`을 검색하면 사회복지 분류가 근거로 잡혔다.

## 변경 계획

1. `ORDER BY`의 최종 tiebreak `cu.unit_code` 직전에 "본가 분류 우선" 키를 추가한다.
2. 판정은 능력단위코드 앞 8자리와 분류의 `major/middle/small/sub` 코드 연결값 비교로 한다.
3. 기존 tier·점수·CASE 순위는 변경하지 않는다. 새 키는 앞선 기준이 모두 동률일 때만 작동한다.
4. DB와 온톨로지 데이터는 수정하지 않는다. 차용 관계는 NCS 원본 구조이므로 보존한다.
5. 로컬과 Vercel 미러를 byte 동일하게 유지한다.

## 구현 결과

`src/ncs_mcp/search/core.py`와 Vercel 미러의 능력단위 질의에 다음 정렬 키를 추가했다.

```sql
CASE
    WHEN SUBSTR(cu.unit_code, 1, 8) =
         COALESCE(c.major_code, '')
         || COALESCE(c.middle_code, '')
         || COALESCE(c.small_code, '')
         || COALESCE(c.sub_code, '')
    THEN 0
    ELSE 1
END,
```

분류명이 질의에 직접 등장하는 경우는 기존 CASE(`THEN 3`)가 먼저 처리하므로, 자원봉사 문맥으로
검색하면 차용본이 여전히 상위에 온다. 새 키는 그 CASE가 동률일 때만 개입한다.

### 영향 범위

전체 13,435행 중 base 코드가 중복되는 그룹은 **1개**(`0202010110` 총무보안관리)뿐이다.
따라서 다른 어떤 질의의 결과도 변하지 않는다. 실제 서빙 DB로 14개 질의를 재실행해
`사옥 보안 점검` 외 13개가 원격 응답과 완전히 동일함을 확인했다.

### 변경 파일

- `src/ncs_mcp/search/core.py`
- `deploy/vercel_mcp_app/src/ncs_mcp/search/core.py`
- `tests/test_ncs_search_recall.py`
- `CHANGELOG.md`
- `reports/debug_2026-09/06_shared_unit_home_classification.md`

## 검색 품질

평가 조건은 동일한 40개 질의, `scope=unit`, `limit=10`이다.

| 버전 | Hit@1 | Hit@3 | MRR@10 | 검색 오류 |
|---|---:|---:|---:|---:|
| 5단계 | 0.8250 | 0.8750 | 0.8550 | 0 |
| 6단계 | 0.8500 | 0.8750 | 0.8675 | 0 |
| 변화 | +0.0250 | 0.0000 | +0.0125 | 0 |

| 카테고리 | Hit@1 | Hit@3 | MRR@10 |
|---|---:|---:|---:|
| 인사 | 0.8750 | 0.8750 | 0.8750 |
| 노무 | 0.8750 | 1.0000 | 0.9375 |
| 교육 | 0.8750 | 0.8750 | 0.8750 |
| 총무 | 0.7500 | 0.7500 | 0.7750 |
| 회계 | 0.8750 | 0.8750 | 0.8750 |

정답이 2위에서 1위로 이동한 결과이므로 Hit@3는 그대로이고 MRR은 `0.5 / 40 = 0.0125`만큼
증가했다. 총무 카테고리 Hit@1은 `1 / 8 = 0.125` 증가했다. 수치가 산술적으로 일치한다.

## 검증 결과

- 원격 프로덕션 재검증: intent alias 11개, 타 분야 반례 3개 모두 기대대로 동작.
- 검색·벤치마크·payload·배포 parity 통합 실행: 95개 통과.
- `tests/test_ncs_search_recall.py`: 19개 통과. 신규 회귀 테스트 2개 포함.
  - `test_shared_unit_ranks_home_classification_above_borrowed_copy`
  - `test_borrowing_classification_context_still_surfaces_borrowed_copy`
- 자연어 평가: 40건, 검색 오류 0건, `--enforce-hit3` 게이트 `pass`.
- `python scripts/ncs_harness.py lint`: 오류 0, 경고 0.
- `python scripts/ncs_harness.py smoke`: 통과.
- `python scripts/ncs_harness.py ontology validate`: 통과, 이슈 0개.
- 로컬/Vercel 미러 byte parity 통과.
- MCP 계약 변경 없음. `mcp/ncs-tool-contract.json` 미변경.

## 위험 및 후속 과제

- 현재 중복 base 코드가 1개뿐이라 영향이 좁지만, 향후 NCS 개정으로 차용 능력단위가 늘면
  같은 tiebreak가 더 많은 그룹에 작용한다. 그때는 본가 우선이 항상 옳은지 재검토해야 한다.
- 차용본을 의도적으로 찾고 싶은 사용자를 위해, 분류를 지정하는 검색 파라미터가 있으면
  더 안전하다. 현재는 분류명을 질의에 포함하는 방식에 의존한다.
- 40개 평가 세트는 여전히 alias 설계에 사용된 세트이므로 독립 holdout 구축 과제는 유효하다.
