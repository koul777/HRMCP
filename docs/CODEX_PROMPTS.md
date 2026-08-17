# NCS MCP — Codex 작업 프롬프트 모음

각 프롬프트는 독립 세션에서 하나씩 실행하세요.
DB 쓰기 작업은 반드시 `--dry-run` 결과를 확인한 뒤 승인하세요.

---

## PROMPT 1 — 현재 상태 진단 (항상 먼저 실행)

```
Read ARCHITECTURE.md and .agents/data-system-improvement-agent.md in full before doing anything else.

Then run the following commands in order and collect all output:

1. python scripts/ncs_harness.py lint
2. python scripts/ncs_harness.py smoke
3. python scripts/ncs_harness.py quality-gates --out reports/gates_diagnosis_current.json --markdown-out reports/gates_diagnosis_current.md
4. python scripts/ncs_harness.py qualification-error-report --out reports/qual_error_current.json --markdown-out reports/qual_error_current.md

After all commands finish, summarize:
- lint: pass or fail (list any errors)
- smoke: pass or fail
- quality gates: total pass/warn/fail counts, list every warn and fail gate with its current value and threshold
- qualification errors: how many units are in error state, what error types

Do NOT modify any files or DB until I review this summary.
```

---

## PROMPT 2 — 소스 파일 잘림 & 문법 오류 전수 검사

```
Read ARCHITECTURE.md first.

Run this Python snippet to check all tracked Python files for syntax errors and truncation:

python3 - << 'EOF'
import ast, subprocess, sys

result = subprocess.run(
    ["git", "status", "--short"],
    capture_output=True, text=True
)
files = [
    line.split()[-1]
    for line in result.stdout.splitlines()
    if line.strip().endswith(".py")
]

issues = []
for path in files:
    try:
        with open(path, "rb") as f:
            content = f.read()
        if not content.endswith(b"\n"):
            issues.append(f"TRUNCATED (no trailing newline): {path}")
        ast.parse(content.decode("utf-8", errors="replace"))
    except SyntaxError as e:
        issues.append(f"SYNTAX ERROR {path} line {e.lineno}: {e.msg}")
    except Exception as e:
        issues.append(f"ERROR {path}: {e}")

if issues:
    for i in issues:
        print(i)
else:
    print("All files OK")
EOF

For every file reported as TRUNCATED or SYNTAX ERROR:
1. Show the last 20 lines of that file
2. Find the nearest complete function or class boundary just before the cut
3. Check git log to see if the file existed in a previous commit: git show HEAD:<path>
4. Reconstruct the missing tail following the existing code patterns in that file
5. Append ONLY the missing portion — do not rewrite the whole file

After all fixes, re-run the syntax check to confirm zero issues.
Save a summary to reports/file_integrity_fix_YYYYMMDD.md.
```

---

## PROMPT 3 — 미커밋 변경사항 정리 및 커밋

```
Read ARCHITECTURE.md first.

Step 1 — validate:
  python scripts/ncs_harness.py lint
  python scripts/ncs_harness.py smoke

If either fails, stop and report the errors. Do NOT proceed to commit.

Step 2 — show what will be staged:
  git status --short | grep -v "^?"

Review this list carefully. Do NOT stage:
  - Any *.db, *.db-wal, *.db-shm, *.db-journal file
  - Any file under data/processed/
  - Any file under data/raw/
  - Any *.log file
  - Any tmp/ directory contents

Step 3 — stage only safe files:
  git add -u

Verify staged list with: git diff --cached --name-only

Step 4 — commit:
  git commit -m "Incremental: fix truncated source files, harness syntax, ontology pipeline updates"

Step 5 — show final status:
  git log --oneline -5
  git status --short | head -20

Report how many files were committed and how many untracked files remain.
```

---

## PROMPT 4 — Qualification 수집 재개 (22% → 90%)

```
Read ARCHITECTURE.md and .agents/data-collection-agent.md in full first.

Step 1 — check current collection state:
  python scripts/ncs_harness.py qualification-error-report \
    --out reports/qual_before_collect_YYYYMMDD.json \
    --markdown-out reports/qual_before_collect_YYYYMMDD.md

Report: how many units are collected / empty / error, and current coverage %.

Step 2 — retry units currently in error state (rate-limited ones first):
  python scripts/ncs_harness.py retry-qualification-errors \
    --out reports/qual_retry_YYYYMMDD.json

Wait for completion. Report how many succeeded, how many are still failing.

Step 3 — collect remaining uncollected units:
  python scripts/ncs_harness.py collect-qualification-items \
    --all-units \
    --limit-units 100 \
    --num-of-rows 50 \
    --max-pages 1 \
    --request-delay 2 \
    --max-retries 1 \
    --retry-backoff-seconds 30 \
    --stop-after-rate-limit-errors 3 \
    --ncs006-checkpoint-path reports/checkpoint_ncs006_element_api_status_YYYYMMDD_current.json

Rules during collection:
  - If you see HTTP 429 (rate limited), stop immediately and report how many were collected before the limit
  - Do NOT loop endlessly on errors — stop after 3 consecutive failures on the same unit
  - Save progress report every 500 units

Step 4 — after collection finishes, run quality gates again:
  python scripts/ncs_harness.py quality-gates \
    --out reports/gates_after_qual_collect_YYYYMMDD.json \
    --markdown-out reports/gates_after_qual_collect_YYYYMMDD.md

Report the new qualification:collection_coverage value.
Target is >= 0.90. If still below, report how many units remain and what's blocking them.
```

---

## PROMPT 5 — api_element_unmatched 진단 및 수정

```
Read ARCHITECTURE.md and .agents/data-system-improvement-agent.md in full first.

Step 1 — diagnose the issue:
Run this query against data/processed/ncs.db and show results:

python3 - << 'EOF'
import sqlite3
conn = sqlite3.connect("data/processed/ncs.db")
conn.row_factory = sqlite3.Row

# Total count
total = conn.execute(
    "SELECT COUNT(*) FROM quality_issues WHERE issue_type = 'api_element_unmatched'"
).fetchone()[0]
print(f"Total api_element_unmatched: {total}")

# Sample issues
rows = conn.execute("""
    SELECT target_type, target_id, issue_detail
    FROM quality_issues
    WHERE issue_type = 'api_element_unmatched'
    LIMIT 10
""").fetchall()
for r in rows:
    print(dict(r))

# Distribution by target_type
dist = conn.execute("""
    SELECT target_type, COUNT(*) as cnt
    FROM quality_issues
    WHERE issue_type = 'api_element_unmatched'
    GROUP BY target_type ORDER BY cnt DESC
""").fetchall()
for r in dist:
    print(dict(r))
conn.close()
EOF

Step 2 — based on the sample output, identify the root cause:
  - Are these element names from the API that don't match any element_id in competency_elements?
  - Or something else?

Step 3 — propose ONE specific fix. Options might be:
  A. A normalization step that fuzzy-matches API element names to DB element names
  B. A bulk-delete of stale quality_issues that no longer apply
  C. A schema change to store unmatched elements separately

Show the proposed fix as a dry-run BEFORE touching the DB.
Write the proposal to reports/element_unmatched_fix_proposal_YYYYMMDD.md.

Do NOT apply any DB changes until I review and approve the proposal.
```

---

## PROMPT 6 — Human Review 준비 시트 생성

```
Read ARCHITECTURE.md and .agents/task-ksa-review-agent.md in full first.

IMPORTANT: This task generates review materials only.
Do NOT set human_reviewed, accepted, or reviewed status on any record.
Do NOT write to ontology_concepts, ksa_concept_links, or any review status field.

Step 1 — generate the ontology review queue (dry run):
  python scripts/ncs_harness.py prepare-ontology-review-queue \
    --dry-run \
    --concept-limit 50 \
    --out reports/ontology_review_dryrun_YYYYMMDD.json

Step 2 — export a CSV review sheet for the top 50 concepts:
python3 - << 'EOF'
import sqlite3, csv
conn = sqlite3.connect("data/processed/ncs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT
        oc.concept_id,
        oc.concept_name,
        oc.concept_type,
        oc.definition_status,
        oc.definition,
        oc.review_status,
        COUNT(DISTINCT pc.criteria_id) AS criteria_count,
        COUNT(DISTINCT cu.unit_code) AS unit_count
    FROM ontology_concepts oc
    JOIN ksa_atomic_concept_links acl ON acl.concept_id = oc.concept_id
    JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
    JOIN element_criteria_ksa_links eck ON eck.ksa_id = atom.ksa_id
    JOIN performance_criteria pc ON pc.criteria_id = eck.criteria_id
    JOIN competency_elements ce ON ce.element_id = pc.element_id
    JOIN competency_units cu ON cu.unit_code = ce.unit_code
    WHERE oc.review_status != 'human_reviewed'
      AND oc.concept_type IN ('knowledge', 'skill', 'attitude')
    GROUP BY oc.concept_id
    ORDER BY criteria_count DESC, unit_count DESC
    LIMIT 50
""").fetchall()
with open("reports/human_review_batch_01.csv", "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "concept_id", "concept_name", "concept_type",
        "definition_status", "definition", "review_status",
        "criteria_count", "unit_count",
        "REVIEWER_DECISION",  # 사람이 채울 컬럼: accept / reject / edit
        "REVIEWER_DEFINITION",  # 사람이 채울 컬럼: 정의 수정
        "REVIEWER_NOTE"
    ])
    writer.writeheader()
    for r in rows:
        row = dict(r)
        row["REVIEWER_DECISION"] = ""
        row["REVIEWER_DEFINITION"] = ""
        row["REVIEWER_NOTE"] = ""
        writer.writerow(row)
print(f"Saved {len(rows)} rows to reports/human_review_batch_01.csv")
conn.close()
EOF

Step 3 — save a markdown summary:
  python scripts/ncs_harness.py review-priority \
    --out reports/review_priority_YYYYMMDD.json \
    --markdown-out reports/review_priority_YYYYMMDD.md

Report: how many concepts need review, breakdown by concept_type and definition_status.
The CSV file is ready for human review. No DB changes were made.
```

---

## PROMPT 7 — 12GB DB 배포 전략 설계 (코드 작성 없음)

```
Read ARCHITECTURE.md section "SQLite Operational Boundary" in full first.

Analyze the current DB and propose a distribution strategy for open-source release.

Step 1 — measure the DB:
python3 - << 'EOF'
import sqlite3
conn = sqlite3.connect("data/processed/ncs.db")
conn.row_factory = sqlite3.Row
tables = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()
for t in tables:
    name = t["name"]
    count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(f"{name}: {count:,} rows")
conn.close()
EOF

Step 2 — write a comparison of these 3 options to reports/db_distribution_options_YYYYMMDD.md:

Option A: Sample DB (2 대분류만 포함)
  - Which tables and rows would be included?
  - Estimated size
  - User friction: can they test the MCP immediately?
  - Maintenance: do we need to regenerate when schema changes?

Option B: Build-from-scratch script
  - What prerequisites does the user need? (API key, time, disk space)
  - Estimated collection time for full 24 대분류
  - Risk: NCS API rate limits, availability

Option C: Git LFS + GitHub Release asset
  - Storage cost on GitHub LFS
  - Download experience for users
  - What happens when DB grows beyond current 12GB?

Option D: Hybrid — sample DB bundled + build script for full data
  - Complexity vs user experience tradeoff

For each option, score on: user_friction (1-5), maintenance_cost (1-5), storage_cost (1-5).
Recommend ONE option with rationale.
Do not implement anything — this is planning only.
```

---

## PROMPT 8 — 오픈소스 릴리즈 체크리스트 생성

```
Read ARCHITECTURE.md, README.md, and .agents/README.md in full first.
Also run: python scripts/ncs_harness.py quality-gates --out /dev/null

Generate a release readiness checklist at reports/opensource_release_checklist_YYYYMMDD.md with these sections:

## 코드 품질
- [ ] lint pass
- [ ] smoke pass
- [ ] unittest discover -s tests -v (0 failures)
- [ ] No truncated source files
- [ ] All changes committed

## 데이터 완성도
- [ ] qualification:collection_coverage >= 0.90 (현재: ?)
- [ ] api_element_unmatched <= 200 (현재: ?)
- [ ] human_reviewed_concepts > 0 (현재: 0)

## 배포 준비
- [ ] DB 배포 전략 결정 및 문서화
- [ ] .env.example 최신화
- [ ] API 키 발급 방법 README에 명시
- [ ] Docker build 성공 확인

## 문서
- [ ] README.md: 설치 → 실행까지 5단계 이내로 가능한지 확인
- [ ] ARCHITECTURE.md 최신 상태 반영 여부
- [ ] CHANGELOG.md 존재 여부

Fill in current values where measurable.
Mark each item PASS / FAIL / UNKNOWN.
Count total: X/Y items passing.
Do not fix anything — assessment only.
```

---

## 실행 순서 권장

1. **PROMPT 1** — 현재 상태 진단 (필수, 항상 먼저)
2. **PROMPT 2** — 파일 잘림 수정
3. **PROMPT 3** — 커밋 정리
4. **PROMPT 4** — Qualification 수집 (시간 오래 걸림, 별도 세션)
5. **PROMPT 5** — element unmatched 진단 → 별도 승인 후 수정
6. **PROMPT 6** — Human review 시트 생성 → 직접 검토
7. **PROMPT 7** — DB 배포 전략 결정 → 직접 선택
8. **PROMPT 8** — 릴리즈 체크리스트 최종 확인
