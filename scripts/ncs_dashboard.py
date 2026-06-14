from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.refinement import apply_refinement_to_target


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NCS-SQF Ontology Workbench</title>
  <style>
    :root {
      --bg:#f5f7fb; --panel:#fff; --ink:#172033; --muted:#667085;
      --line:#d9e0ea; --dark:#111827; --accent:#2563eb; --ok:#047857;
      --warn:#b45309; --bad:#b91c1c;
    }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--ink); }
    header { background:var(--dark); color:#fff; padding:18px 24px; display:flex; justify-content:space-between; align-items:center; }
    header h1 { margin:0; font-size:20px; }
    main { max-width:1600px; margin:0 auto; padding:20px 24px 44px; }
    section { margin-top:18px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
    input, select, button, textarea { border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:#fff; font:inherit; }
    input.code { width:72px; }
    input.keyword { width:220px; }
    button { cursor:pointer; background:#1f2937; color:#fff; border-color:#1f2937; }
    button.secondary { background:#fff; color:#1f2937; }
    button.link { background:#fff; color:var(--accent); border-color:#c7d2fe; }
    .muted { color:var(--muted); }
    .ok { color:var(--ok); font-weight:600; }
    .warn { color:var(--warn); font-weight:600; }
    .bad { color:var(--bad); font-weight:600; }
    .cards { display:grid; gap:12px; grid-template-columns:repeat(5, minmax(0, 1fr)); }
    .card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:13px; cursor:pointer; min-height:112px; }
    .card:hover { border-color:var(--accent); box-shadow:0 1px 8px rgba(37,99,235,.14); }
    .card.active { border-color:var(--accent); outline:2px solid rgba(37,99,235,.18); }
    .card .label { color:var(--muted); font-size:12px; }
    .card .value { font-size:26px; font-weight:700; margin:4px 0; }
    .card .sub { color:var(--muted); font-size:12px; line-height:1.35; }
    .split { display:grid; gap:14px; grid-template-columns:minmax(0, 1.45fr) minmax(440px, .85fr); align-items:start; }
    .scroll { overflow:auto; max-height:620px; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:#fff; }
    th, td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    th { position:sticky; top:0; background:#f9fafb; z-index:1; }
    tr:hover td { background:#fbfdff; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; font-size:12px; background:#fff; }
    .detail-box { white-space:pre-wrap; background:#f9fafb; border:1px solid var(--line); border-radius:8px; padding:12px; margin-top:10px; max-height:220px; overflow:auto; }
    textarea { width:100%; min-height:210px; min-width:420px; line-height:1.55; resize:vertical; }
    textarea.small { min-height:84px; }
    .field { margin-top:12px; }
    .field label { display:block; font-size:13px; color:var(--muted); margin-bottom:5px; }
    .summary { display:grid; gap:12px; grid-template-columns:repeat(6, minmax(150px,1fr)); }
    .summary .panel { min-height:88px; }
    .summary strong { display:block; font-size:24px; margin-top:4px; }
    @media (max-width:1180px) {
      .cards { grid-template-columns:repeat(3, minmax(0,1fr)); }
      .split { grid-template-columns:1fr; }
    }
    @media (max-width:720px) {
      .cards, .summary { grid-template-columns:1fr; }
      textarea { min-width:280px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>NCS-SQF 온톨로지 워크벤치</h1>
    <div id="stamp" class="muted"></div>
  </header>
  <main>
    <section class="panel">
      <div class="toolbar">
        <strong>범위</strong>
        <input id="majorCode" class="code" value="" placeholder="대" title="대분류코드">
        <input id="middleCode" class="code" value="" placeholder="중" title="중분류코드">
        <input id="smallCode" class="code" value="" placeholder="소" title="소분류코드">
        <input id="subCode" class="code" value="" placeholder="세" title="세분류코드">
        <input id="keyword" class="keyword" placeholder="능력단위/요소/문장 검색">
        <button onclick="refreshAll()">조회</button>
        <button class="secondary" onclick="clearScope()">전체 NCS</button>
        <button class="secondary" onclick="setHrScope()">인사 직무</button>
        <button class="secondary" onclick="setManagementSupportMvp()">경영지원 MVP</button>
        <span id="liveStatus" class="muted"></span>
      </div>
      <div class="muted">기본값은 전체 NCS입니다. 경영지원 MVP는 SQF `02 > 경영관리 > 경영지원`을 우선 범위로 보고, NCS `02 경영·회계·사무`와 연결합니다.</div>
    </section>

    <section class="summary" id="summary"></section>

    <section class="panel">
      <div class="toolbar">
        <strong>온톨로지 준비 전처리 단계</strong>
        <span class="muted">각 단계의 완료/잔여 작업과 산출 방식을 확인합니다.</span>
      </div>
      <div class="scroll" style="max-height:360px;">
        <table>
          <thead>
            <tr><th>단계</th><th>의미</th><th>완료</th><th>남은 작업</th><th>방법/산출물</th><th>보기</th></tr>
          </thead>
          <tbody id="phases"></tbody>
        </table>
      </div>
    </section>

    <section class="cards" id="cards"></section>

    <section class="split">
      <div class="panel">
        <div class="toolbar">
          <strong id="listTitle">항목 리스트</strong>
          <span id="listMeta" class="muted"></span>
          <button class="secondary" onclick="loadCurrentItems()">새로고침</button>
        </div>
        <div class="scroll">
          <table>
            <thead>
              <tr><th>상태</th><th>코드/ID</th><th>분류/맥락</th><th>원문/내용</th><th>작업</th></tr>
            </thead>
            <tbody id="items"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="toolbar">
          <strong>상세 / 수작업 전처리</strong>
          <span id="detailKind" class="muted"></span>
        </div>
        <div id="emptyDetail" class="muted">왼쪽 리스트에서 항목을 선택하세요.</div>
        <div id="detail" style="display:none;">
          <div class="field">
            <label>맥락</label>
            <div id="context" class="detail-box"></div>
          </div>
          <div class="field">
            <label>원문 명칭</label>
            <div id="titleRaw" class="detail-box"></div>
          </div>
          <div class="field" id="titleEditWrap">
            <label>정제 명칭</label>
            <textarea id="titleRefined" class="small"></textarea>
          </div>
          <div class="field">
            <label>원문/정의/내용</label>
            <div id="bodyRaw" class="detail-box"></div>
          </div>
          <div class="field" id="bodyEditWrap">
            <label>정제 내용</label>
            <textarea id="bodyRefined"></textarea>
          </div>
          <div class="toolbar" style="margin-top:12px;">
            <button onclick="saveCurrentDetail()">수작업 전처리 저장</button>
            <button class="secondary" onclick="loadCurrentItems()">목록 갱신</button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>품질 이슈</strong>
        <select id="targetType">
          <option value="">전체 대상</option>
          <option value="criteria">수행준거</option>
          <option value="ksa">KSA</option>
          <option value="element">능력단위요소</option>
          <option value="unit">능력단위</option>
        </select>
        <select id="issueType"></select>
        <button onclick="loadIssues()">이슈 조회</button>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>ID</th><th>유형</th><th>대상</th><th>심각도</th><th>내용</th><th>작업</th></tr>
          </thead>
          <tbody id="issues"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const fmt = new Intl.NumberFormat('ko-KR');
    const q = (id) => document.getElementById(id);
    let currentCard = {kind:'classification', state:'processed', title:'분류 전처리 완료'};
    let currentDetail = null;
    let overviewTimer = null;

    async function api(path, options={}) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }
    function esc(text) {
      return String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function scopeParams(includeKeyword=true) {
      const params = new URLSearchParams();
      for (const [id, key] of [['majorCode','major_code'], ['middleCode','middle_code'], ['smallCode','small_code'], ['subCode','sub_code']]) {
        const v = q(id).value.trim();
        if (v) params.set(key, v);
      }
      const kw = q('keyword').value.trim();
      if (includeKeyword && kw) params.set('keyword', kw);
      params.set('limit', '100');
      return params;
    }
    function clearScope() {
      for (const id of ['majorCode', 'middleCode', 'smallCode', 'subCode']) q(id).value = '';
      refreshAll();
    }
    function setHrScope() {
      q('majorCode').value = '02';
      q('middleCode').value = '02';
      q('smallCode').value = '02';
      q('subCode').value = '01';
      refreshAll();
    }
    function setManagementSupportMvp() {
      q('majorCode').value = '02';
      q('middleCode').value = '';
      q('smallCode').value = '';
      q('subCode').value = '';
      q('keyword').value = '경영지원';
      refreshAll();
    }
    function statusClass(status) {
      if (['matched','human_reviewed','processed','mapped_source','training'].includes(status)) return 'ok';
      if (['api_failed','error'].includes(status)) return 'bad';
      if (['not_collected','no_data','raw','warning','needs_review','no_training'].includes(status)) return 'warn';
      return '';
    }
    function statusPill(status) {
      return `<span class="pill ${statusClass(status)}">${esc(status || '')}</span>`;
    }

    async function refreshAll() {
      await loadStatus();
      await loadProgress();
      await loadWorkbench();
      await loadCurrentItems();
      await loadIssues();
    }

    async function refreshOverview() {
      await loadStatus();
      await loadProgress();
      await loadWorkbench();
      await loadIssues();
    }

    function scheduleAutoRefresh() {
      if (overviewTimer) clearInterval(overviewTimer);
      overviewTimer = setInterval(() => {
        refreshOverview().catch(err => {
          q('liveStatus').textContent = `자동갱신 오류: ${err.message}`;
        });
      }, 30000);
    }

    async function loadStatus() {
      const data = await api('/api/status');
      const loadedAt = new Date().toLocaleTimeString('ko-KR');
      q('stamp').textContent = `DB ${data.generated_at} / 화면 ${loadedAt}`;
      q('liveStatus').textContent = `자동갱신 30초 / 마지막 ${loadedAt}`;
      const cp = data.counts;
      const ep = data.element_progress;
      const sqf = data.sqf;
      const onto = data.ontology;
      q('summary').innerHTML = [
        `<div class="panel"><span class="muted">능력단위</span><strong>${fmt.format(cp.competency_units)}</strong><span class="ok">API matched ${fmt.format(data.unit_api_status.matched || 0)}</span></div>`,
        `<div class="panel"><span class="muted">능력단위요소 API 검증</span><strong>${ep.percent.toFixed(1)}%</strong><span class="muted">${fmt.format(ep.matched)} / ${fmt.format(ep.total)}</span></div>`,
        `<div class="panel"><span class="muted">SQF 직무수준</span><strong>${fmt.format(cp.sqf_duties || 0)}</strong><span class="muted">제공 대분류 ${fmt.format(sqf.major_codes_with_data || 0)}개</span></div>`,
        `<div class="panel"><span class="muted">경영지원 MVP</span><strong>${fmt.format(sqf.management_support_duties || 0)}</strong><span class="${sqf.management_support_duties ? 'ok' : 'warn'}">SQF 경영지원 직무</span></div>`,
        `<div class="panel"><span class="muted">NCS-SQF 매핑</span><strong>${fmt.format(onto.matches || 0)}</strong><span class="${onto.match_table_present ? 'warn' : 'bad'}">${onto.match_table_present ? '후보 검토 필요' : '테이블 생성 필요'}</span></div>`,
        `<div class="panel"><span class="muted">열린 품질 이슈</span><strong>${fmt.format(data.quality.open_issues)}</strong><span class="muted">resolved ${fmt.format(data.quality.resolved_issues)}</span></div>`
      ].join('');
      const issueTypes = [''].concat(data.issue_types);
      q('issueType').innerHTML = issueTypes.map(v => `<option value="${esc(v)}">${esc(v || '전체 이슈')}</option>`).join('');
    }

    async function loadProgress() {
      const data = await api('/api/progress?' + scopeParams(false).toString());
      q('phases').innerHTML = data.phases.map(phase => `<tr>
        <td><b>${esc(phase.name)}</b><br>${statusPill(phase.status)}</td>
        <td>${esc(phase.meaning)}</td>
        <td>${fmt.format(phase.completed)} / ${fmt.format(phase.total)}<br><span class="muted">${phase.percent.toFixed(1)}%</span></td>
        <td class="${phase.remaining ? 'warn' : 'ok'}">${fmt.format(phase.remaining)}<br><span class="muted">${esc(phase.remaining_detail || '')}</span></td>
        <td>${esc(phase.method)}</td>
        <td><button class="link" onclick="selectCard('${esc(phase.kind)}','${esc(phase.state)}','${esc(phase.title)}')">내역 보기</button></td>
      </tr>`).join('');
    }

    async function loadWorkbench() {
      const data = await api('/api/workbench?' + scopeParams(false).toString());
      q('cards').innerHTML = data.cards.map(card => {
        const active = card.kind === currentCard.kind && card.state === currentCard.state ? ' active' : '';
        return `<div class="card${active}" onclick="selectCard('${esc(card.kind)}','${esc(card.state)}','${esc(card.title)}')">
          <div class="label">${esc(card.group)}</div>
          <div class="value">${fmt.format(card.count)}</div>
          <div><b>${esc(card.title)}</b></div>
          <div class="sub">${esc(card.description)}</div>
        </div>`;
      }).join('');
    }

    async function selectCard(kind, state, title) {
      currentCard = {kind, state, title};
      await loadWorkbench();
      await loadCurrentItems();
    }

    async function loadCurrentItems() {
      const params = scopeParams(true);
      params.set('kind', currentCard.kind);
      params.set('state', currentCard.state);
      const data = await api('/api/items?' + params.toString());
      q('listTitle').textContent = currentCard.title;
      q('listMeta').textContent = `${data.total}건 중 ${data.items.length}건 표시`;
      q('items').innerHTML = data.items.map(item => `<tr>
        <td>${statusPill(item.status)}<br>${item.api_status ? statusPill(item.api_status) : ''}</td>
        <td><b>${esc(item.id)}</b><br><span class="muted">${esc(item.code || '')}</span></td>
        <td>${esc(item.context || '')}</td>
        <td><b>${esc(item.title || '')}</b><br><span class="muted">${esc(item.body || '').slice(0, 260)}</span></td>
        <td><button class="link" onclick="loadDetail('${esc(item.kind)}','${esc(item.id)}')">상세/정제</button></td>
      </tr>`).join('');
      if (!data.items.length) {
        q('items').innerHTML = '<tr><td colspan="5" class="muted">조회 결과가 없습니다.</td></tr>';
      }
    }

    async function loadDetail(kind, id) {
      const data = await api('/api/item-detail?kind=' + encodeURIComponent(kind) + '&id=' + encodeURIComponent(id));
      currentDetail = data.item;
      q('emptyDetail').style.display = 'none';
      q('detail').style.display = 'block';
      q('detailKind').textContent = `${currentDetail.kind} / ${currentDetail.id}`;
      q('context').textContent = currentDetail.context || '';
      q('titleRaw').textContent = currentDetail.title_raw || '';
      q('bodyRaw').textContent = currentDetail.body_raw || '';
      q('titleRefined').value = currentDetail.title_refined || currentDetail.title_raw || '';
      q('bodyRefined').value = currentDetail.body_refined || currentDetail.body_raw || '';
      q('titleEditWrap').style.display = currentDetail.can_refine_title ? 'block' : 'none';
      q('bodyEditWrap').style.display = currentDetail.can_refine_body ? 'block' : 'none';
    }

    async function saveCurrentDetail() {
      if (!currentDetail) return;
      if (!currentDetail.can_refine_title && !currentDetail.can_refine_body) {
        alert('이 항목은 읽기 전용 근거입니다.');
        return;
      }
      await api('/api/preprocess', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          kind: currentDetail.kind,
          id: currentDetail.id,
          title_refined: q('titleRefined').value,
          body_refined: q('bodyRefined').value
        })
      });
      await loadDetail(currentDetail.kind, currentDetail.id);
      await loadCurrentItems();
      await loadStatus();
      await loadWorkbench();
    }

    async function loadIssues() {
      const params = new URLSearchParams();
      params.set('limit', '100');
      if (q('targetType').value) params.set('target_type', q('targetType').value);
      if (q('issueType').value) params.set('issue_type', q('issueType').value);
      const data = await api('/api/issues?' + params.toString());
      q('issues').innerHTML = data.issues.map(item => `<tr>
        <td>${item.issue_id}</td>
        <td>${esc(item.issue_type)}</td>
        <td>${esc(item.target_type)}<br>${esc(item.target_id)}</td>
        <td class="${statusClass(item.severity)}">${esc(item.severity)}</td>
        <td><b>${esc(item.unit_code || '')}</b> ${esc(item.unit_name || '')}<br>${esc(item.raw_text || item.issue_detail || '')}</td>
        <td><button class="link" onclick="loadDetail('${esc(item.target_type)}','${esc(item.target_id)}')">상세/정제</button><br><button class="secondary" onclick="resolveIssue(${item.issue_id})">해결 처리</button></td>
      </tr>`).join('');
      if (!data.issues.length) q('issues').innerHTML = '<tr><td colspan="6" class="muted">열린 이슈가 없습니다.</td></tr>';
    }

    async function resolveIssue(issueId) {
      await api('/api/resolve', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({issue_id: issueId})
      });
      await loadIssues();
      await loadStatus();
      await loadWorkbench();
    }

    refreshAll().then(scheduleAutoRefresh).catch(err => alert(err.message));
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NcsDashboard/0.3"

    def json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def html_response(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.html_response()
            elif parsed.path == "/api/status":
                self.json_response(get_status(self.server.db_path))
            elif parsed.path == "/api/progress":
                self.json_response(get_progress(self.server.db_path, params))
            elif parsed.path == "/api/workbench":
                self.json_response(get_workbench(self.server.db_path, params))
            elif parsed.path == "/api/items":
                self.json_response(get_items(self.server.db_path, params))
            elif parsed.path == "/api/item-detail":
                self.json_response(get_item_detail(self.server.db_path, params))
            elif parsed.path == "/api/classifications":
                self.json_response(get_classifications(self.server.db_path, params))
            elif parsed.path == "/api/units":
                self.json_response(get_units(self.server.db_path, params))
            elif parsed.path == "/api/unit":
                self.json_response(get_unit_detail(self.server.db_path, params))
            elif parsed.path == "/api/api-orphans":
                self.json_response(get_api_orphans(self.server.db_path, params))
            elif parsed.path == "/api/issues":
                self.json_response(get_issues(self.server.db_path, params))
            else:
                self.json_response({"error": "not_found"}, status=404)
        except Exception as exc:
            self.json_response({"error": type(exc).__name__, "detail": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        payload = json.loads(body)
        try:
            if parsed.path == "/api/preprocess":
                self.json_response(save_manual_preprocess(self.server.db_path, payload))
            elif parsed.path == "/api/refine":
                self.json_response(save_refined(self.server.db_path, payload))
            elif parsed.path == "/api/resolve":
                self.json_response(resolve_issue(self.server.db_path, payload))
            elif parsed.path == "/api/review-mapping":
                self.json_response(review_mapping_candidate(self.server.db_path, payload))
            elif parsed.path == "/api/review-refinement":
                self.json_response(review_refinement_job(self.server.db_path, payload))
            else:
                self.json_response({"error": "not_found"}, status=404)
        except Exception as exc:
            self.json_response({"error": type(exc).__name__, "detail": str(exc)}, status=500)

    def log_message(self, format: str, *args) -> None:
        return


def connect_db(db_path: Path):
    conn = connect(db_path)
    initialize_database(conn)
    return conn


def scalar(conn, sql: str, params: list | tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    return params.get(key, [default])[0].strip()


def safe_limit(params: dict[str, list[str]], default: int = 100, maximum: int = 500) -> int:
    try:
        return max(1, min(int(first(params, "limit", str(default))), maximum))
    except ValueError:
        return default


def classification_filters(params: dict[str, list[str]], alias: str = "c") -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    for field in ["major_code", "middle_code", "small_code", "sub_code"]:
        value = first(params, field)
        if value:
            clauses.append(f"{alias}.{field} = ?")
            values.append(value)
    return clauses, values


def scoped_where(params: dict[str, list[str]], alias: str = "c", extra: list[str] | None = None) -> tuple[str, list[str]]:
    clauses, values = classification_filters(params, alias)
    if extra:
        clauses.extend(extra)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", values)


def get_status(db_path: Path) -> dict:
    conn = connect_db(db_path)
    counts = {
        table: scalar(conn, f"SELECT COUNT(*) FROM {table}")
        for table in [
            "raw_excel_rows",
            "classifications",
            "competency_units",
            "competency_elements",
            "performance_criteria",
            "ksa_items",
            "api_raw_responses",
            "api_competency_units",
            "sqf_duties",
            "quality_issues",
        ]
    }
    match_table_present = table_exists(conn, "sqf_ncs_matches")
    match_count = scalar(conn, "SELECT COUNT(*) FROM sqf_ncs_matches") if match_table_present else 0
    reviewed_match_count = (
        scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM sqf_ncs_matches
            WHERE review_status IN ('reviewed', 'human_reviewed', 'accepted')
            """,
        )
        if match_table_present
        else 0
    )
    sqf_major_codes = scalar(
        conn,
        "SELECT COUNT(DISTINCT ncs_lclas_cd) FROM sqf_duties",
    )
    sqf_management_support = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
        """,
    )
    sqf_with_education = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE duty_education_training IS NOT NULL
          AND TRIM(duty_education_training) <> ''
          AND TRIM(duty_education_training) <> '-'
        """,
    )
    element_status = {
        row["api_match_status"]: row["count"]
        for row in conn.execute(
            "SELECT api_match_status, COUNT(*) AS count FROM competency_elements GROUP BY api_match_status"
        )
    }
    unit_status = {
        row["api_match_status"]: row["count"]
        for row in conn.execute(
            "SELECT api_match_status, COUNT(*) AS count FROM competency_units GROUP BY api_match_status"
        )
    }
    total_elements = counts["competency_elements"]
    matched = int(element_status.get("matched", 0))
    issue_types = [
        row["issue_type"]
        for row in conn.execute(
            """
            SELECT issue_type
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY issue_type
            ORDER BY issue_type
            """
        )
    ]
    payload = {
        "generated_at": now_utc(),
        "counts": counts,
        "unit_api_status": unit_status,
        "element_api_status": element_status,
        "element_progress": {
            "total": total_elements,
            "matched": matched,
            "not_collected": int(element_status.get("not_collected", 0)),
            "api_failed": int(element_status.get("api_failed", 0)),
            "no_data": int(element_status.get("no_data", 0)),
            "percent": (matched / total_elements * 100) if total_elements else 0,
        },
        "quality": {
            "open_issues": scalar(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL"),
            "resolved_issues": scalar(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NOT NULL"),
        },
        "sqf": {
            "major_codes_with_data": sqf_major_codes,
            "management_support_duties": sqf_management_support,
            "duties_with_training": sqf_with_education,
        },
        "ontology": {
            "match_table_present": match_table_present,
            "matches": match_count,
            "reviewed_matches": reviewed_match_count,
        },
        "issue_types": issue_types,
        "missing_duty_definitions": scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM classifications
            WHERE duty_def_api IS NULL OR TRIM(duty_def_api) = ''
            """,
        ),
    }
    conn.close()
    return payload


def count_query(conn, sql: str, values: list[str]) -> int:
    return scalar(conn, sql, values)


def percent(completed: int, total: int) -> float:
    return (completed / total * 100) if total else 100.0


def phase(
    *,
    name: str,
    meaning: str,
    completed: int,
    total: int,
    remaining: int,
    remaining_detail: str,
    method: str,
    kind: str,
    state: str,
    title: str,
) -> dict:
    return {
        "name": name,
        "meaning": meaning,
        "completed": completed,
        "total": total,
        "remaining": remaining,
        "remaining_detail": remaining_detail,
        "percent": percent(completed, total),
        "status": "complete" if remaining == 0 else "in_progress",
        "method": method,
        "kind": kind,
        "state": state,
        "title": title,
    }


def get_progress(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db(db_path)
    where_c, vals_c = scoped_where(params, "c")
    where_r, vals_r = scoped_where(params, "r")

    raw_rows = count_query(conn, f"SELECT COUNT(*) FROM raw_excel_rows r {where_r}", vals_r)
    classifications = count_query(conn, f"SELECT COUNT(*) FROM classifications c {where_c}", vals_c)
    duty_defs = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM classifications c
        {where_c} {"AND" if where_c else "WHERE"} c.duty_def_api IS NOT NULL AND TRIM(c.duty_def_api) <> ''
        """,
        vals_c,
    )
    units = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    units_matched = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} cu.api_match_status = 'matched'
        """,
        vals_c,
    )
    elements = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    elements_matched = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'matched'
        """,
        vals_c,
    )
    elements_not_collected = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'not_collected'
        """,
        vals_c,
    )
    elements_problem = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status IN ('api_failed', 'no_data')
        """,
        vals_c,
    )
    criteria = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    criteria_refined = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} pc.criteria_text_refined IS NOT NULL AND TRIM(pc.criteria_text_refined) <> ''
        """,
        vals_c,
    )
    ksa = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    ksa_refined = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ki.ksa_text_refined IS NOT NULL AND TRIM(ki.ksa_text_refined) <> ''
        """,
        vals_c,
    )
    open_issues = count_query(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL", [])
    resolved_issues = count_query(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NOT NULL", [])
    all_issues = open_issues + resolved_issues
    sqf_total = count_query(conn, "SELECT COUNT(*) FROM sqf_duties", [])
    sqf_major_codes = count_query(conn, "SELECT COUNT(DISTINCT ncs_lclas_cd) FROM sqf_duties", [])
    sqf_mvp = count_query(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
        """,
        [],
    )
    sqf_mvp_with_definition = count_query(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
          AND duty_definition IS NOT NULL
          AND TRIM(duty_definition) <> ''
        """,
        [],
    )
    match_table_present = table_exists(conn, "sqf_ncs_matches")
    sqf_matches = count_query(conn, "SELECT COUNT(*) FROM sqf_ncs_matches", []) if match_table_present else 0
    sqf_reviewed_matches = (
        count_query(
            conn,
            """
            SELECT COUNT(*)
            FROM sqf_ncs_matches
            WHERE review_status IN ('reviewed', 'human_reviewed', 'accepted')
            """,
            [],
        )
        if match_table_present
        else 0
    )

    phases = [
        phase(
            name="Excel 계층 정규화",
            meaning="플랫 Excel 행을 분류, 능력단위, 요소, 수행준거, KSA 테이블로 분리",
            completed=raw_rows,
            total=raw_rows,
            remaining=0 if raw_rows else 1,
            remaining_detail="원천 행 적재 완료" if raw_rows else "원천 Excel 적재 필요",
            method="중복 제거 + 계층 키 생성 + raw 원문 보존",
            kind="classification",
            state="processed",
            title="분류 전처리 완료",
        ),
        phase(
            name="능력단위 API 매칭",
            meaning="NCS005 기준정보와 Excel 능력단위를 능력단위 코드로 조인",
            completed=units_matched,
            total=units,
            remaining=max(units - units_matched, 0),
            remaining_detail="미매칭 능력단위 확인",
            method="NCS_CL_CD / unit_code 코드 매칭, API 정의 저장",
            kind="unit",
            state="api_matched" if units_matched else "processed",
            title="능력단위 API matched",
        ),
        phase(
            name="직무정의 API 보강",
            meaning="세분류/직무 정의를 API에서 받아 분류 테이블에 보강",
            completed=duty_defs,
            total=classifications,
            remaining=max(classifications - duty_defs, 0),
            remaining_detail="직무정의 누락 분류 확인",
            method="NCS004 DUTY_DEF 저장",
            kind="classification",
            state="processed",
            title="분류 전처리 완료",
        ),
        phase(
            name="능력단위요소 API 검증",
            meaning="Excel 요소가 NCS006 기준정보와 일치하는지 검증",
            completed=elements_matched,
            total=elements,
            remaining=max(elements - elements_matched, 0),
            remaining_detail=f"not_collected {elements_not_collected:,}, failed/no_data {elements_problem:,}",
            method="요소 번호 단위 API 조회, matched/api_failed/no_data 상태 저장",
            kind="element",
            state="api_not_collected" if elements_not_collected else "api_problem",
            title="요소 API 미수집" if elements_not_collected else "요소 API 실패/없음",
        ),
        phase(
            name="SQF 직무수준 수집",
            meaning="SQF openapi26 산업별 직무와 직무수준을 NCS 대분류 코드로 적재",
            completed=sqf_major_codes,
            total=24,
            remaining=max(24 - sqf_major_codes, 0),
            remaining_detail=f"제공 대분류 {sqf_major_codes:,}개, SQF 직무수준 {sqf_total:,}건",
            method="NCS_SQF_SERVICE_KEY + /openapi26, code 000 정상, 002 빈 데이터",
            kind="sqf",
            state="all",
            title="SQF 직무수준 전체",
        ),
        phase(
            name="경영지원 MVP 범위",
            meaning="1차 MVP를 SQF 02 > 경영관리 > 경영지원 직무로 제한",
            completed=sqf_mvp_with_definition,
            total=sqf_mvp,
            remaining=max(sqf_mvp - sqf_mvp_with_definition, 0),
            remaining_detail=f"경영지원 SQF 직무수준 {sqf_mvp:,}건",
            method="SQF job_name='경영지원'을 NCS 02 경영·회계·사무와 연결",
            kind="sqf",
            state="mvp",
            title="경영지원 MVP SQF 직무",
        ),
        phase(
            name="NCS-SQF 매핑 객체",
            meaning="SQF 직무수준과 NCS 능력단위/KSA 사이의 관계, 점수, 근거, 버전 저장",
            completed=sqf_reviewed_matches,
            total=max(sqf_matches, 1),
            remaining=(max(sqf_matches - sqf_reviewed_matches, 0) if match_table_present else 1),
            remaining_detail=(
                f"후보 {sqf_matches:,}건, 검토 {sqf_reviewed_matches:,}건"
                if match_table_present
                else "sqf_ncs_matches 테이블 생성 필요"
            ),
            method="sameAs 금지, requires/closeMatch/partiallyCovers + evidence/confidence 저장",
            kind="sqf",
            state="mvp",
            title="경영지원 MVP SQF 직무",
        ),
        phase(
            name="사람 수작업 정제",
            meaning="수행준거와 KSA의 정제본을 원문과 별도 저장",
            completed=criteria_refined + ksa_refined,
            total=criteria + ksa,
            remaining=max((criteria + ksa) - (criteria_refined + ksa_refined), 0),
            remaining_detail=f"수행준거 미정제 {criteria - criteria_refined:,}, KSA 미정제 {ksa - ksa_refined:,}",
            method="raw 필드 보존, refined 필드와 review_status만 갱신",
            kind="criteria",
            state="raw",
            title="수행준거 미정제",
        ),
        phase(
            name="품질 이슈 검토",
            meaning="중복, 누락, 짧은 문장 등 품질 진단 결과 처리",
            completed=resolved_issues,
            total=all_issues,
            remaining=open_issues,
            remaining_detail="열린 이슈를 상세 확인하거나 해결 처리",
            method="quality_issues 테이블 기반 Human-in-the-loop 검토",
            kind="quality",
            state="open",
            title="열린 품질 이슈",
        ),
    ]
    conn.close()
    return {"phases": phases}


def get_workbench(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db(db_path)
    where_c, vals_c = scoped_where(params, "c")
    cards = [
        {
            "group": "DB 전처리 완료",
            "title": "분류 전처리 완료",
            "kind": "classification",
            "state": "processed",
            "count": count_query(conn, f"SELECT COUNT(*) FROM classifications c {where_c}", vals_c),
            "description": "정규화된 세분류/직무 리스트",
        },
        {
            "group": "DB 전처리 완료",
            "title": "능력단위 전처리 완료",
            "kind": "unit",
            "state": "processed",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c}
                """,
                vals_c,
            ),
            "description": "Excel에서 정규화된 능력단위",
        },
        {
            "group": "DB 전처리 완료",
            "title": "능력단위요소 전처리 완료",
            "kind": "element",
            "state": "processed",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c}
                """,
                vals_c,
            ),
            "description": "Excel에서 정규화된 요소",
        },
        {
            "group": "수작업 전처리 필요",
            "title": "수행준거 미정제",
            "kind": "criteria",
            "state": "raw",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} (pc.criteria_text_refined IS NULL OR TRIM(pc.criteria_text_refined) = '')
                """,
                vals_c,
            ),
            "description": "사람이 정제본을 입력할 수 있는 수행준거",
        },
        {
            "group": "수작업 전처리 필요",
            "title": "KSA 미정제",
            "kind": "ksa",
            "state": "raw",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} (ki.ksa_text_refined IS NULL OR TRIM(ki.ksa_text_refined) = '')
                """,
                vals_c,
            ),
            "description": "사람이 정제본을 입력할 수 있는 KSA",
        },
        {
            "group": "API 매칭 완료",
            "title": "능력단위 API matched",
            "kind": "unit",
            "state": "api_matched",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} cu.api_match_status = 'matched'
                """,
                vals_c,
            ),
            "description": "NCS005/API 정의가 연결된 능력단위",
        },
        {
            "group": "API 매칭 완료",
            "title": "요소 API matched",
            "kind": "element",
            "state": "api_matched",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'matched'
                """,
                vals_c,
            ),
            "description": "NCS006/API 요소명이 검증된 요소",
        },
        {
            "group": "API 미처리",
            "title": "요소 API 미수집",
            "kind": "element",
            "state": "api_not_collected",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'not_collected'
                """,
                vals_c,
            ),
            "description": "아직 API 검증을 돌리지 않은 요소",
        },
        {
            "group": "API 미처리",
            "title": "요소 API 실패/없음",
            "kind": "element",
            "state": "api_problem",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status IN ('api_failed', 'no_data')
                """,
                vals_c,
            ),
            "description": "사람이 원문 확인하거나 재수집 후보로 볼 요소",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "SQF 직무수준 전체",
            "kind": "sqf",
            "state": "all",
            "count": count_query(conn, "SELECT COUNT(*) FROM sqf_duties", []),
            "description": "openapi26에서 수집한 SQF 산업별 직무수준",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "경영지원 MVP SQF 직무",
            "kind": "sqf",
            "state": "mvp",
            "count": count_query(
                conn,
                """
                SELECT COUNT(*)
                FROM sqf_duties
                WHERE ncs_lclas_cd = '02'
                  AND sqf_field_name = '경영관리'
                  AND job_name = '경영지원'
                """,
                [],
            ),
            "description": "1차 MVP 범위: 02 경영관리 > 경영지원",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "직접 교육훈련 근거 있음",
            "kind": "sqf",
            "state": "training",
            "count": count_query(
                conn,
                """
                SELECT COUNT(*)
                FROM sqf_duties
                WHERE duty_education_training IS NOT NULL
                  AND TRIM(duty_education_training) <> ''
                  AND TRIM(duty_education_training) <> '-'
                """,
                [],
            ),
            "description": "SQF dutyEduTrain이 직접 채워진 직무수준",
        },
        {
            "group": "품질 검토",
            "title": "열린 품질 이슈",
            "kind": "quality",
            "state": "open",
            "count": count_query(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL", []),
            "description": "품질 진단에서 발견된 검토 항목",
        },
    ]
    conn.close()
    return {"cards": cards}


def keyword_clause(params: dict[str, list[str]], fields: list[str]) -> tuple[str, list[str]]:
    keyword = first(params, "keyword")
    if not keyword:
        return "", []
    clause = "(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")"
    return clause, [f"%{keyword}%" for _ in fields]


def state_clause(kind: str, state: str, alias: str) -> str:
    if state == "api_matched":
        return f"{alias}.api_match_status = 'matched'"
    if state == "api_not_collected":
        return f"{alias}.api_match_status = 'not_collected'"
    if state == "api_problem":
        return f"{alias}.api_match_status IN ('api_failed', 'no_data')"
    if state == "raw" and kind == "criteria":
        return "(pc.criteria_text_refined IS NULL OR TRIM(pc.criteria_text_refined) = '')"
    if state == "raw" and kind == "ksa":
        return "(ki.ksa_text_refined IS NULL OR TRIM(ki.ksa_text_refined) = '')"
    if state == "refined" and kind == "criteria":
        return "(pc.criteria_text_refined IS NOT NULL AND TRIM(pc.criteria_text_refined) <> '')"
    if state == "refined" and kind == "ksa":
        return "(ki.ksa_text_refined IS NOT NULL AND TRIM(ki.ksa_text_refined) <> '')"
    return ""


def get_items(db_path: Path, params: dict[str, list[str]]) -> dict:
    kind = first(params, "kind", "classification")
    state = first(params, "state", "processed")
    limit = safe_limit(params)
    conn = connect_db(db_path)
    if kind == "classification":
        where, values = scoped_where(params, "c")
        rows = conn.execute(
            f"""
            SELECT 'classification' AS kind, c.classification_id AS id,
                   c.major_code || '-' || c.middle_code || '-' || c.small_code || '-' || c.sub_code AS code,
                   c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name AS context,
                   c.sub_name AS title, COALESCE(c.duty_def_refined, c.duty_def_api, '') AS body,
                   c.review_status AS status, c.api_usg_yn AS api_status
            FROM classifications c
            {where}
            ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(conn, f"SELECT COUNT(*) FROM classifications c {where}", values)
    elif kind == "unit":
        extra = []
        state_sql = state_clause(kind, state, "cu")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["cu.unit_code", "cu.unit_name_raw", "cu.api_definition"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'unit' AS kind, cu.unit_code AS id, cu.unit_code AS code,
                   c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name || ' > ' || c.sub_name AS context,
                   COALESCE(cu.unit_name_refined, cu.unit_name_raw) AS title,
                   COALESCE(cu.api_definition_refined, cu.api_definition, '') AS body,
                   cu.review_status AS status, cu.api_match_status AS api_status
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "element":
        extra = []
        state_sql = state_clause(kind, state, "ce")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["ce.element_name_raw", "ce.api_element_name", "ce.unit_code"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'element' AS kind, ce.element_id AS id, ce.unit_code || ' #' || ce.element_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw AS context,
                   COALESCE(ce.element_name_refined, ce.element_name_raw) AS title,
                   COALESCE(ce.api_element_name, '') AS body,
                   ce.review_status AS status, ce.api_match_status AS api_status
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY ce.unit_code, CAST(ce.element_no AS INTEGER), ce.element_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "criteria":
        extra = []
        state_sql = state_clause(kind, state, "pc")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["pc.criteria_text_raw", "ce.element_name_raw", "cu.unit_name_raw"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'criteria' AS kind, pc.criteria_id AS id, '수행준거 ' || pc.criteria_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw || ' > ' || ce.element_name_raw AS context,
                   ce.element_name_raw AS title,
                   COALESCE(pc.criteria_text_refined, pc.criteria_text_raw) AS body,
                   pc.review_status AS status, ce.api_match_status AS api_status
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code, ce.element_no, CAST(pc.criteria_no AS INTEGER), pc.criteria_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "ksa":
        extra = []
        state_sql = state_clause(kind, state, "ki")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["ki.ksa_text_raw", "ki.ksa_type_name", "ce.element_name_raw", "cu.unit_name_raw"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'ksa' AS kind, ki.ksa_id AS id, ki.ksa_type_name || ' ' || ki.ksa_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw || ' > ' || ce.element_name_raw AS context,
                   ce.element_name_raw AS title,
                   COALESCE(ki.ksa_text_refined, ki.ksa_text_raw) AS body,
                   ki.review_status AS status, ce.api_match_status AS api_status
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code, ce.element_no, ki.ksa_type_code, CAST(ki.ksa_no AS INTEGER), ki.ksa_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "sqf":
        clauses: list[str] = []
        values: list[str] = []
        major_code = first(params, "major_code")
        if major_code:
            clauses.append("sd.ncs_lclas_cd = ?")
            values.append(major_code)
        if state == "mvp":
            clauses.extend(
                [
                    "sd.ncs_lclas_cd = '02'",
                    "sd.sqf_field_name = '경영관리'",
                    "sd.job_name = '경영지원'",
                ]
            )
        elif state == "training":
            clauses.append(
                """
                sd.duty_education_training IS NOT NULL
                AND TRIM(sd.duty_education_training) <> ''
                AND TRIM(sd.duty_education_training) <> '-'
                """
            )
        kw, kw_vals = keyword_clause(
            params,
            [
                "sd.ncs_lclas_name",
                "sd.sqf_field_name",
                "sd.job_name",
                "sd.duty_name",
                "sd.duty_definition",
                "sd.duty_qualification",
                "sd.duty_career",
            ],
        )
        if kw:
            clauses.append(kw)
            values.extend(kw_vals)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT 'sqf' AS kind, sd.source_key AS id,
                   sd.ncs_lclas_cd || ' ' || sd.ncs_lclas_name AS code,
                   sd.sqf_field_name || ' > ' || sd.job_name AS context,
                   sd.duty_name || CASE WHEN sd.duty_level <> '' THEN ' / Level ' || sd.duty_level ELSE '' END AS title,
                   COALESCE(sd.duty_definition, sd.duty_level_name, '') AS body,
                   CASE
                     WHEN sd.duty_definition IS NOT NULL AND TRIM(sd.duty_definition) <> '' THEN 'mapped_source'
                     ELSE 'needs_review'
                   END AS status,
                   CASE
                     WHEN sd.duty_education_training IS NOT NULL
                       AND TRIM(sd.duty_education_training) <> ''
                       AND TRIM(sd.duty_education_training) <> '-'
                     THEN 'training'
                     ELSE 'no_training'
                   END AS api_status
            FROM sqf_duties sd
            {where}
            ORDER BY sd.ncs_lclas_cd, sd.sqf_field_name, sd.job_name, CAST(sd.duty_level AS INTEGER), sd.duty_name
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(conn, f"SELECT COUNT(*) FROM sqf_duties sd {where}", values)
    elif kind == "quality":
        rows = conn.execute(
            """
            SELECT 'quality' AS kind, qi.issue_id AS id, qi.target_type || ':' || qi.target_id AS code,
                   qi.issue_type AS context, qi.severity AS title, qi.issue_detail AS body,
                   qi.severity AS status, '' AS api_status
            FROM quality_issues qi
            WHERE qi.resolved_at IS NULL
            ORDER BY CASE qi.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, qi.issue_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = count_query(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL", [])
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    conn.close()
    return {"kind": kind, "state": state, "total": total, "items": [dict(row) for row in rows]}


def get_item_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    kind = first(params, "kind")
    item_id = first(params, "id")
    if not kind or not item_id:
        raise ValueError("kind and id are required")
    conn = connect_db(db_path)
    row = None
    item: dict
    if kind == "classification":
        row = conn.execute(
            """
            SELECT c.*, c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name AS context
            FROM classifications c
            WHERE c.classification_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": row["context"],
                "title_raw": row["sub_name"],
                "title_refined": row["sub_name"],
                "body_raw": row["duty_def_api"] or "",
                "body_refined": row["duty_def_refined"] or "",
                "can_refine_title": False,
                "can_refine_body": True,
                "status": row["review_status"],
            }
        else:
            item = {}
    elif kind == "unit":
        row = conn.execute(
            """
            SELECT cu.*, c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name || ' > ' || c.sub_name AS context
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": row["context"],
                "title_raw": row["unit_name_raw"],
                "title_refined": row["unit_name_refined"] or "",
                "body_raw": row["api_definition"] or "",
                "body_refined": row["api_definition_refined"] or "",
                "can_refine_title": True,
                "can_refine_body": True,
                "status": row["review_status"],
                "api_status": row["api_match_status"],
            }
        else:
            item = {}
    elif kind == "element":
        row = conn.execute(
            """
            SELECT ce.*, cu.unit_name_raw, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE ce.element_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}",
                "title_raw": row["element_name_raw"],
                "title_refined": row["element_name_refined"] or "",
                "body_raw": row["api_element_name"] or "",
                "body_refined": row["api_element_name"] or "",
                "can_refine_title": True,
                "can_refine_body": False,
                "status": row["review_status"],
                "api_status": row["api_match_status"],
            }
        else:
            item = {}
    elif kind == "criteria":
        row = conn.execute(
            """
            SELECT pc.*, ce.element_name_raw, cu.unit_code, cu.unit_name_raw, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE pc.criteria_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}\n{row['element_name_raw']}",
                "title_raw": f"수행준거 {row['criteria_no']}",
                "title_refined": f"수행준거 {row['criteria_no']}",
                "body_raw": row["criteria_text_raw"],
                "body_refined": row["criteria_text_refined"] or "",
                "can_refine_title": False,
                "can_refine_body": True,
                "status": row["review_status"],
            }
        else:
            item = {}
    elif kind == "ksa":
        row = conn.execute(
            """
            SELECT ki.*, ce.element_name_raw, cu.unit_code, cu.unit_name_raw, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE ki.ksa_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}\n{row['element_name_raw']}",
                "title_raw": f"{row['ksa_type_name']} {row['ksa_no']}",
                "title_refined": f"{row['ksa_type_name']} {row['ksa_no']}",
                "body_raw": row["ksa_text_raw"],
                "body_refined": row["ksa_text_refined"] or "",
                "can_refine_title": False,
                "can_refine_body": True,
                "status": row["review_status"],
            }
        else:
            item = {}
    elif kind == "sqf":
        row = conn.execute(
            """
            SELECT *
            FROM sqf_duties
            WHERE source_key = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            evidence = [
                f"직무정의: {row['duty_definition'] or ''}",
                f"직무수준 정의: {row['duty_level_name'] or ''}",
                f"자율성과 책임성: {row['autonomy_responsibility'] or ''}",
                f"교육훈련: {row['duty_education_training'] or ''}",
                f"자격: {row['duty_qualification'] or ''}",
                f"경력: {row['duty_career'] or ''}",
                f"면허: {row['duty_license'] or ''}",
                f"비고: {row['duty_remark'] or ''}",
            ]
            item = {
                "kind": kind,
                "id": item_id,
                "context": (
                    f"{row['ncs_lclas_cd']} {row['ncs_lclas_name']}\n"
                    f"{row['sqf_field_name']} > {row['job_name']}"
                ),
                "title_raw": f"{row['duty_name']} / Level {row['duty_level']}",
                "title_refined": f"{row['duty_name']} / Level {row['duty_level']}",
                "body_raw": "\n".join(evidence),
                "body_refined": "",
                "can_refine_title": False,
                "can_refine_body": False,
                "status": "mapped_source" if row["duty_definition"] else "needs_review",
                "api_status": "training" if row["duty_education_training"] else "no_training",
            }
        else:
            item = {}
    elif kind == "quality":
        row = conn.execute("SELECT * FROM quality_issues WHERE issue_id = ?", (item_id,)).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['target_type']}:{row['target_id']}",
                "title_raw": row["issue_type"],
                "title_refined": row["issue_type"],
                "body_raw": row["issue_detail"],
                "body_refined": row["suggested_action"] or "",
                "can_refine_title": False,
                "can_refine_body": False,
                "status": row["severity"],
            }
        else:
            item = {}
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    conn.close()
    if not item:
        return {"error": "not_found", "kind": kind, "id": item_id}
    return {"item": item}


def save_manual_preprocess(db_path: Path, payload: dict) -> dict:
    kind = str(payload["kind"])
    item_id = str(payload["id"])
    title_refined = str(payload.get("title_refined", "")).strip()
    body_refined = str(payload.get("body_refined", "")).strip()
    conn = connect_db(db_path)
    if kind == "classification":
        conn.execute(
            """
            UPDATE classifications
            SET duty_def_refined = ?, review_status = 'human_reviewed'
            WHERE classification_id = ?
            """,
            (body_refined, item_id),
        )
    elif kind == "unit":
        conn.execute(
            """
            UPDATE competency_units
            SET unit_name_refined = ?, api_definition_refined = ?, review_status = 'human_reviewed', updated_at = ?
            WHERE unit_code = ?
            """,
            (title_refined, body_refined, now_utc(), item_id),
        )
    elif kind == "element":
        conn.execute(
            """
            UPDATE competency_elements
            SET element_name_refined = ?, review_status = 'human_reviewed'
            WHERE element_id = ?
            """,
            (title_refined, item_id),
        )
    elif kind == "criteria":
        conn.execute(
            """
            UPDATE performance_criteria
            SET criteria_text_refined = ?, review_status = 'human_reviewed'
            WHERE criteria_id = ?
            """,
            (body_refined, item_id),
        )
    elif kind == "ksa":
        conn.execute(
            """
            UPDATE ksa_items
            SET ksa_text_refined = ?, review_status = 'human_reviewed'
            WHERE ksa_id = ?
            """,
            (body_refined, item_id),
        )
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    issue_id = payload.get("issue_id")
    if issue_id:
        conn.execute("UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?", (now_utc(), issue_id))
    conn.commit()
    conn.close()
    return {"ok": True, "kind": kind, "id": item_id}


def insert_review_audit(
    conn,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    previous_status: str | None,
    new_status: str | None,
    reviewer_id: str | None,
    notes: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO review_audit_log(
            entity_type, entity_id, action, previous_status,
            new_status, reviewer_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            previous_status,
            new_status,
            reviewer_id,
            notes,
            now_utc(),
        ),
    )


def review_mapping_candidate(db_path: Path, payload: dict) -> dict:
    match_id = str(payload["match_id"])
    action = str(payload["action"])
    reviewer_id = str(payload.get("reviewer_id", "dashboard")).strip() or "dashboard"
    notes = str(payload.get("notes", "")).strip()
    relation = str(payload.get("relation", "")).strip()
    allowed = {
        "accept": "accepted",
        "reject": "rejected",
        "mark_low_confidence": "low_confidence",
        "revise_relation": "revised",
    }
    if action not in allowed:
        raise ValueError(f"unsupported mapping review action: {action}")
    conn = connect_db(db_path)
    row = conn.execute("SELECT * FROM sqf_ncs_matches WHERE match_id = ?", (match_id,)).fetchone()
    if row is None:
        conn.close()
        return {"error": "not_found", "match_id": match_id}
    previous = row["review_status"]
    new_status = allowed[action]
    updates = [
        "review_status = ?",
        "reviewer_id = ?",
        "reviewed_at = ?",
        "reviewer_notes = ?",
        "updated_at = ?",
    ]
    values = [new_status, reviewer_id, now_utc(), notes, now_utc()]
    if action == "revise_relation":
        if not relation:
            conn.close()
            raise ValueError("relation is required for revise_relation")
        updates.append("relation = ?")
        values.append(relation)
    values.append(match_id)
    conn.execute(f"UPDATE sqf_ncs_matches SET {', '.join(updates)} WHERE match_id = ?", values)
    insert_review_audit(
        conn,
        entity_type="sqf_ncs_match",
        entity_id=match_id,
        action=action,
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
    )
    conn.commit()
    conn.close()
    return {"ok": True, "match_id": match_id, "previous_status": previous, "new_status": new_status}


def review_refinement_job(db_path: Path, payload: dict) -> dict:
    job_id = str(payload["job_id"])
    action = str(payload["action"])
    reviewer_id = str(payload.get("reviewer_id", "dashboard")).strip() or "dashboard"
    notes = str(payload.get("notes", "")).strip()
    if action not in {"approve_refined", "reject_refined", "edit_refined"}:
        raise ValueError(f"unsupported refinement review action: {action}")
    conn = connect_db(db_path)
    row = conn.execute("SELECT * FROM refinement_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        conn.close()
        return {"error": "not_found", "job_id": job_id}
    previous = row["review_status"]
    if action == "reject_refined":
        new_status = "rejected"
        conn.execute("UPDATE refinement_jobs SET review_status = ? WHERE job_id = ?", (new_status, job_id))
    else:
        refined_text = str(payload.get("refined_text") or row["refined_text"] or "").strip()
        if not refined_text:
            conn.close()
            raise ValueError("refined_text is required")
        apply_refinement_to_target(
            conn,
            target_type=row["target_type"],
            target_id=row["target_id"],
            refined_text=refined_text,
            review_status="human_reviewed",
        )
        new_status = "applied"
        conn.execute(
            """
            UPDATE refinement_jobs
            SET refined_text = ?, rationale = ?, review_status = ?, applied_at = ?
            WHERE job_id = ?
            """,
            (refined_text, notes or row["rationale"], new_status, now_utc(), job_id),
        )
    insert_review_audit(
        conn,
        entity_type="refinement_job",
        entity_id=job_id,
        action=action,
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
    )
    conn.commit()
    conn.close()
    return {"ok": True, "job_id": job_id, "previous_status": previous, "new_status": new_status}


def get_classifications(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params)
    conn = connect_db(db_path)
    rows = conn.execute(
        f"""
        SELECT
            c.*,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM classifications c
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        {where}
        GROUP BY c.classification_id
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    conn.close()
    return {"classifications": [dict(row) for row in rows]}


def get_units(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    keyword = first(params, "keyword")
    status = first(params, "api_match_status")
    if keyword:
        clauses.append("(cu.unit_code LIKE ? OR cu.unit_name_raw LIKE ? OR cu.api_definition LIKE ?)")
        values.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if status:
        clauses.append("cu.api_match_status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params)
    conn = connect_db(db_path)
    rows = conn.execute(
        f"""
        SELECT
            cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
            cu.unit_name_refined, cu.api_unit_name, cu.api_unit_level,
            cu.api_definition, cu.api_definition_refined, cu.api_match_status,
            c.major_name, c.middle_name, c.small_name, c.sub_name,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'matched' THEN ce.element_id END) AS element_matched,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        {where}
        GROUP BY cu.unit_code
        ORDER BY cu.unit_code
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    conn.close()
    return {"units": [dict(row) for row in rows]}


def get_unit_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    unit_code = first(params, "unit_code")
    if not unit_code:
        raise ValueError("unit_code is required")
    conn = connect_db(db_path)
    unit = conn.execute(
        """
        SELECT cu.*, c.major_name, c.middle_name, c.small_name, c.sub_name, c.duty_def_api, c.duty_def_refined
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE cu.unit_code = ?
        """,
        (unit_code,),
    ).fetchone()
    if unit is None:
        conn.close()
        return {"error": "not_found", "unit_code": unit_code}
    elements = conn.execute(
        """
        SELECT
            ce.*,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM competency_elements ce
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code = ?
        GROUP BY ce.element_id
        ORDER BY CAST(ce.element_no AS INTEGER), ce.element_id
        """,
        (unit_code,),
    ).fetchall()
    conn.close()
    return {"unit": dict(unit), "elements": [dict(row) for row in elements]}


def get_api_orphans(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params)
    conn = connect_db(db_path)
    rows = conn.execute(
        """
        SELECT acu.*
        FROM api_competency_units acu
        LEFT JOIN competency_units cu ON cu.unit_code = acu.ncs_cl_cd
        WHERE cu.unit_code IS NULL
        ORDER BY acu.ncs_cl_cd
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return {"api_orphans": [dict(row) for row in rows]}


def get_issues(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params, default=100)
    clauses = ["qi.resolved_at IS NULL"]
    sql_params: list[str | int] = []
    for field in ["target_type", "issue_type", "severity"]:
        value = first(params, field)
        if value:
            clauses.append(f"qi.{field} = ?")
            sql_params.append(value)
    where = " AND ".join(clauses)
    conn = connect_db(db_path)
    rows = conn.execute(
        f"""
        SELECT *
        FROM quality_issues qi
        WHERE {where}
        ORDER BY
          CASE qi.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
          qi.issue_id
        LIMIT ?
        """,
        [*sql_params, limit],
    ).fetchall()
    issues = [enrich_issue(conn, dict(row)) for row in rows]
    conn.close()
    return {"issues": issues}


def enrich_issue(conn, issue: dict) -> dict:
    target_type = issue["target_type"]
    target_id = issue["target_id"]
    if target_type == "criteria":
        row = conn.execute(
            """
            SELECT pc.criteria_text_raw AS raw_text, pc.criteria_text_refined AS refined_text,
                   pc.review_status, ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE pc.criteria_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "ksa":
        row = conn.execute(
            """
            SELECT ki.ksa_text_raw AS raw_text, ki.ksa_text_refined AS refined_text,
                   ki.review_status, ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ki.ksa_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "element":
        row = conn.execute(
            """
            SELECT ce.element_name_raw AS raw_text, ce.element_name_refined AS refined_text,
                   ce.api_match_status AS review_status, ce.element_id,
                   ce.element_name_raw AS element_name, ce.unit_code,
                   cu.unit_name_raw AS unit_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ce.element_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "unit":
        row = conn.execute(
            """
            SELECT cu.unit_name_raw AS raw_text, cu.unit_name_refined AS refined_text,
                   cu.api_match_status AS review_status, cu.unit_code,
                   cu.unit_name_raw AS unit_name
            FROM competency_units cu
            WHERE cu.unit_code = ?
            """,
            (target_id,),
        ).fetchone()
    else:
        row = None
    if row:
        issue.update(dict(row))
    return issue


def save_refined(db_path: Path, payload: dict) -> dict:
    target_type = payload["target_type"]
    target_id = str(payload["target_id"])
    refined_text = str(payload.get("refined_text", "")).strip()
    return save_manual_preprocess(
        db_path,
        {
            "kind": target_type,
            "id": target_id,
            "body_refined": refined_text,
            "title_refined": refined_text,
            "issue_id": payload.get("issue_id"),
        },
    )


def resolve_issue(db_path: Path, payload: dict) -> dict:
    issue_id = payload["issue_id"]
    conn = connect_db(db_path)
    conn.execute("UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?", (now_utc(), issue_id))
    conn.commit()
    conn.close()
    return {"ok": True, "issue_id": issue_id}


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run local NCS MCP dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.db_path = args.db_path
    print(f"NCS MCP dashboard: http://{args.host}:{args.port}")
    print(f"DB: {args.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
