from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    connect,
    ensure_ontology_seeded,
    initialize_database,
    normalize_concept_key,
    now_utc,
)
from ncs_mcp.refinement import apply_refinement_to_target

_DB_PREPARE_LOCK = Lock()
_DB_SCHEMA_PREPARED_PATHS: set[Path] = set()
_DB_ONTOLOGY_PREPARED_PATHS: set[Path] = set()


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
    .taxonomy-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:12px; }
    .taxonomy-head h2 { margin:0; font-size:18px; }
    .taxonomy-head p { margin:4px 0 0; color:var(--muted); }
    .major-grid { display:grid; grid-template-columns:repeat(8, minmax(0,1fr)); gap:10px; }
    .major-tile { min-height:112px; text-align:center; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px 9px; cursor:pointer; }
    .major-tile:hover, .node:hover { border-color:var(--accent); box-shadow:0 1px 8px rgba(37,99,235,.12); }
    .major-tile.active, .node.active { border-color:var(--accent); outline:2px solid rgba(37,99,235,.18); background:#f8fbff; }
    .major-icon { width:42px; height:42px; display:grid; place-items:center; border-radius:8px; background:#e0f2fe; color:#075985; font-size:22px; margin:0 auto 8px; }
    .node-icon { width:28px; height:28px; display:grid; place-items:center; border-radius:7px; background:#f1f5f9; color:#334155; font-size:13px; font-weight:700; flex:0 0 auto; }
    .tile-title { font-weight:700; line-height:1.25; min-height:34px; }
    .tile-meta { font-size:12px; color:var(--muted); margin-top:5px; }
    .progress { height:7px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin-top:8px; }
    .progress > span { display:block; height:100%; background:linear-gradient(90deg,#0ea5e9,#2563eb); width:0; }
    .hierarchy-grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-top:14px; background:#fff; }
    .lane { min-height:360px; border-right:1px solid var(--line); }
    .lane:last-child { border-right:0; }
    .lane h3 { margin:0; padding:12px; font-size:14px; background:#f8fafc; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:8px; }
    .lane-list { max-height:440px; overflow:auto; padding:8px; display:grid; gap:7px; }
    .node { width:100%; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:8px; padding:9px; cursor:pointer; text-align:left; display:flex; gap:9px; align-items:flex-start; }
    .node-main { min-width:0; flex:1; }
    .node-title { display:block; font-weight:700; line-height:1.3; overflow-wrap:anywhere; }
    .node-sub { display:block; font-size:12px; color:var(--muted); margin-top:2px; overflow-wrap:anywhere; }
    .sub-status { margin-top:14px; display:none; border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:14px; }
    .sub-status.visible { display:block; }
    .sub-status h3 { margin:0 0 10px; }
    .status-grid { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:10px; }
    .status-cell { background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px; min-height:82px; }
    .status-cell b { display:block; margin-bottom:4px; }
    .quick-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .ontology-tree { display:grid; gap:10px; }
    .ontology-unit { border:1px solid var(--line); border-radius:8px; background:#fff; overflow:hidden; }
    .ontology-unit > summary { cursor:pointer; padding:12px; font-weight:700; background:#f8fafc; }
    .ontology-body { padding:10px 12px 14px; display:grid; gap:10px; }
    .ontology-element { border-left:3px solid #0ea5e9; padding:8px 0 8px 10px; }
    .ontology-element h4 { margin:0 0 8px; font-size:14px; }
    .ontology-columns { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; }
    .ontology-group { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fbfdff; }
    .ontology-group h5 { margin:0 0 7px; font-size:13px; }
    .ontology-list { display:grid; gap:6px; }
    .ontology-item { width:100%; text-align:left; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:6px; padding:7px; cursor:pointer; }
    .ontology-item:hover { border-color:var(--accent); }
    .ontology-item .muted { display:block; margin-top:2px; }
    .ontology-status { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; }
    .ontology-stat { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; }
    .ontology-stat h3 { margin:0 0 8px; font-size:15px; }
    .ontology-stat button { margin:4px 4px 0 0; }
    details.advanced { margin-top:18px; }
    details.advanced > summary { cursor:pointer; font-weight:700; background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
    details.advanced[open] > summary { border-bottom-left-radius:0; border-bottom-right-radius:0; }
    .guide-row td { background:#fbfdff; color:var(--muted); padding:22px; text-align:center; }
    @media (max-width:1180px) {
      .cards { grid-template-columns:repeat(3, minmax(0,1fr)); }
      .major-grid { grid-template-columns:repeat(4, minmax(0,1fr)); }
      .hierarchy-grid { grid-template-columns:repeat(2, minmax(0,1fr)); }
      .ontology-columns { grid-template-columns:1fr; }
      .lane:nth-child(2) { border-right:0; }
      .lane:nth-child(1), .lane:nth-child(2) { border-bottom:1px solid var(--line); }
      .split { grid-template-columns:1fr; }
    }
    @media (max-width:720px) {
      .cards, .summary, .major-grid, .hierarchy-grid, .status-grid { grid-template-columns:1fr; }
      .lane { border-right:0; border-bottom:1px solid var(--line); }
      .lane:last-child { border-bottom:0; }
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
        <input id="majorCode" class="code" value="" title="대분류코드" placeholder="대">
        <input id="middleCode" class="code" value="" title="중분류코드" placeholder="중">
        <input id="smallCode" class="code" value="" title="소분류코드" placeholder="소">
        <input id="subCode" class="code" value="" title="세분류코드" placeholder="세">
        <input id="keyword" class="keyword" placeholder="능력단위/요소/문장 검색">
        <button onclick="refreshAll()">조회</button>
        <button class="secondary" onclick="clearScope()">전체 NCS</button>
        <button class="secondary" onclick="setHrScope()">인사 직무</button>
        <button class="secondary" onclick="setManagementSupportMvp()">경영지원 MVP</button>
        <span id="liveStatus" class="muted"></span>
      </div>
      <div class="muted">대분류 아이콘을 누르고 중분류, 소분류, 세분류를 차례로 선택하세요. 경영지원 MVP는 SQF `02 > 경영관리 > 경영지원`을 우선 범위로 보고 NCS `02 경영·회계·사무`와 연결합니다.</div>
    </section>

    <section class="summary" id="summary"></section>

    <section class="panel">
      <div class="taxonomy-head">
        <div>
          <h2>NCS 분류 클릭 탐색</h2>
          <p>대분류 아이콘을 누른 뒤 중분류, 소분류, 세분류를 차례로 선택하면 해당 세분류의 전처리 현황과 수작업 보정 목록이 바로 열립니다.</p>
        </div>
        <button class="secondary" onclick="resetTaxonomy()">대분류 다시 선택</button>
      </div>
      <div id="majorTiles" class="major-grid"></div>
      <div class="hierarchy-grid">
        <div class="lane">
          <h3>중분류 <span id="middleMeta" class="muted"></span></h3>
          <div id="middleList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>소분류 <span id="smallMeta" class="muted"></span></h3>
          <div id="smallList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>세분류 <span id="subMeta" class="muted"></span></h3>
          <div id="subList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>능력단위 <span id="unitMeta" class="muted"></span></h3>
          <div id="unitList" class="lane-list"></div>
        </div>
      </div>
      <div id="subStatus" class="sub-status"></div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>선택 범위 온톨로지 구조</strong>
        <span id="ontologyMeta" class="muted"></span>
        <button class="secondary" onclick="loadOntology()">새로고침</button>
      </div>
      <div class="toolbar">
        <strong>온톨로지 구축 워크벤치</strong>
        <span id="ontologyWorkbenchMeta" class="muted"></span>
      </div>
      <div id="ontologyWorkbench" class="ontology-status"></div>
      <div class="scroll" style="max-height:260px; margin:12px 0;">
        <table>
          <thead>
            <tr><th>개념</th><th>유형</th><th>정의</th><th>관계</th><th>별칭</th><th>작업</th></tr>
          </thead>
          <tbody id="conceptWorkItems">
            <tr class="guide-row"><td colspan="6">온톨로지 작업 버튼을 클릭하세요.</td></tr>
          </tbody>
        </table>
      </div>
      <div id="ontologyTree" class="ontology-tree">
        <div class="muted">대분류를 선택하면 능력단위-요소-수행준거-KSA 구조가 표시됩니다.</div>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>Recommendation Audit</strong>
        <button class="secondary" onclick="loadRecommendationRuns()">Reload</button>
        <span id="recommendationMeta" class="muted"></span>
      </div>
      <div class="split">
        <div class="scroll" style="max-height:320px;">
          <table>
            <thead>
              <tr><th>Run</th><th>Query</th><th>Target</th><th>Summary</th><th>Action</th></tr>
            </thead>
            <tbody id="recommendationRuns">
              <tr class="guide-row"><td colspan="5">No recommendation runs loaded.</td></tr>
            </tbody>
          </table>
        </div>
        <div id="recommendationDetail" class="detail-box">Select a recommendation run.</div>
      </div>
    </section>

    <details class="advanced">
      <summary>상세 진행 현황 / 원시 상태 카드 보기</summary>
      <section class="panel" style="border-top:0; border-top-left-radius:0; border-top-right-radius:0;">
        <div class="toolbar">
          <strong>온톨로지 준비 전처리 단계</strong>
          <span class="muted">선택된 분류 범위의 완료/잔여 작업과 산출 방식을 확인합니다.</span>
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
    </details>

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
            <label>현재 상태</label>
            <div id="detailStatus" class="detail-box"></div>
          </div>
          <div class="field">
            <label>맥락</label>
            <div id="context" class="detail-box"></div>
          </div>
          <div class="field">
            <label id="titleRawLabel">원문 명칭</label>
            <div id="titleRaw" class="detail-box"></div>
          </div>
          <div class="field" id="titleEditWrap">
            <label id="titleRefinedLabel">정제 명칭</label>
            <textarea id="titleRefined" class="small"></textarea>
          </div>
          <div class="field">
            <label id="bodyRawLabel">원문/정의/내용</label>
            <div id="bodyRaw" class="detail-box"></div>
          </div>
          <div class="field" id="bodyEditWrap">
            <label id="bodyRefinedLabel">정제 내용</label>
            <textarea id="bodyRefined"></textarea>
          </div>
          <div id="ontologyConceptFields" style="display:none;">
            <div class="field">
              <label>별칭</label>
              <textarea id="conceptAliases" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>상위 개념</label>
              <textarea id="parentConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>하위 개념</label>
              <textarea id="childConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>관련 개념</label>
              <textarea id="relatedConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>관련 수행준거</label>
              <div id="relatedCriteria" class="detail-box"></div>
            </div>
          </div>
          <div class="toolbar" style="margin-top:12px;">
            <button onclick="saveCurrentDetail()">수작업 전처리 저장</button>
            <button class="secondary" onclick="fillRawAsRefined()">원문 그대로 사용</button>
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
    let currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
    let currentDetail = null;
    const majorIcons = {
      '01':'📊','02':'🧾','03':'🏦','04':'🎓','05':'⚖️','06':'🏥',
      '07':'🤝','08':'🎨','09':'🚚','10':'🏷️','11':'🧹','12':'🏨',
      '13':'🍽️','14':'🏗️','15':'⚙️','16':'🧱','17':'🧪','18':'🧵',
      '19':'⚡','20':'📡','21':'🥫','22':'🖨️','23':'🌱','24':'🚜'
    };
    let overviewTimer = null;
    const fallbackMajorNodes = [
      ['01','사업관리'], ['02','경영·회계·사무'], ['03','금융·보험'], ['04','교육·자연·사회과학'],
      ['05','법률·경찰·소방·교도·국방'], ['06','보건·의료'], ['07','사회복지·종교'], ['08','문화·예술·디자인·방송'],
      ['09','운전·운송'], ['10','영업판매'], ['11','경비·청소'], ['12','이용·숙박·여행·오락·스포츠'],
      ['13','음식서비스'], ['14','건설'], ['15','기계'], ['16','재료'],
      ['17','화학·바이오'], ['18','섬유·의복'], ['19','전기·전자'], ['20','정보통신'],
      ['21','식품가공'], ['22','인쇄·목재·가구·공예'], ['23','환경·에너지·안전'], ['24','농림어업']
    ].map(([major_code, name]) => ({major_code, middle_code:'', small_code:'', sub_code:'', code:major_code, name}));

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
      q('keyword').value = '';
      currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
      refreshAll();
    }
    function resetTaxonomy() {
      clearScope();
    }
    function setScope(major='', middle='', small='', sub='') {
      q('majorCode').value = major || '';
      q('middleCode').value = middle || '';
      q('smallCode').value = small || '';
      q('subCode').value = sub || '';
    }
    function selectedCodes() {
      return {
        major: q('majorCode').value.trim(),
        middle: q('middleCode').value.trim(),
        small: q('smallCode').value.trim(),
        sub: q('subCode').value.trim()
      };
    }
    function hasSelectedSub() {
      const codes = selectedCodes();
      return Boolean(codes.major && codes.middle && codes.small && codes.sub);
    }
    function hasSelectedScope() {
      return Boolean(selectedCodes().major);
    }
    function scopeLabel() {
      const codes = selectedCodes();
      const parts = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean);
      return parts.length ? parts.join('-') : '전체 NCS';
    }
    function clearDetail(message='왼쪽 리스트에서 항목을 선택하세요.') {
      currentDetail = null;
      q('detail').style.display = 'none';
      q('detailKind').textContent = '';
      q('emptyDetail').style.display = 'block';
      q('emptyDetail').textContent = message;
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
      if (['matched','human_reviewed','processed','defined','linked','definition:defined','relation:linked','mapped_source','training'].includes(status)) return 'ok';
      if (['api_failed','error'].includes(status)) return 'bad';
      if (['not_collected','no_data','raw','warning','missing','unlinked','definition:missing','relation:unlinked','needs_review','no_training'].includes(status)) return 'warn';
      return '';
    }
    function statusPill(status) {
      return `<span class="pill ${statusClass(status)}">${esc(status || '')}</span>`;
    }
    function progressBar(percent) {
      const value = Math.max(0, Math.min(100, Number(percent || 0)));
      return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
    }
    function taxonomyParams(level) {
      const params = new URLSearchParams();
      const codes = selectedCodes();
      params.set('level', level);
      params.set('limit', level === 'major' ? '100' : '500');
      if (codes.major) params.set('major_code', codes.major);
      if (codes.middle) params.set('middle_code', codes.middle);
      if (codes.small) params.set('small_code', codes.small);
      if (codes.sub) params.set('sub_code', codes.sub);
      return params;
    }
    function renderEmpty(target, message) {
      q(target).innerHTML = `<div class="muted" style="padding:10px;">${esc(message)}</div>`;
    }
    function renderMajorTiles(nodes) {
      const codes = selectedCodes();
      q('majorTiles').innerHTML = nodes.map(node => {
        const active = node.major_code === codes.major ? ' active' : '';
        const hasStats = node.element_count !== undefined && node.element_count !== null;
        const pct = Number(node.element_percent || 0);
        const meta = hasStats
          ? `<div class="tile-meta">요소 API ${pct.toFixed(1)}% · ${fmt.format(node.element_matched)} / ${fmt.format(node.element_count)}</div>${progressBar(pct)}`
          : '<div class="tile-meta">대분류 선택</div>';
        return `<button type="button" class="major-tile${active}" data-major-code="${esc(node.major_code)}" aria-pressed="${active ? 'true' : 'false'}">
          <div class="major-icon">${majorIcons[node.major_code] || node.major_code}</div>
          <div class="tile-title">${esc(node.major_code)}. ${esc(node.name)}</div>
          ${meta}
        </button>`;
      }).join('');
    }
    function renderNodeList(target, metaTarget, nodes, level) {
      const codes = selectedCodes();
      q(metaTarget).textContent = nodes.length ? `${fmt.format(nodes.length)}개` : '';
      if (!nodes.length) {
        const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
        renderEmpty(target, messages[level] || '조회 결과가 없습니다.');
        return;
      }
      q(target).innerHTML = nodes.map(node => {
        const active =
          (level === 'middle' && node.middle_code === codes.middle) ||
          (level === 'small' && node.small_code === codes.small) ||
          (level === 'sub' && node.sub_code === codes.sub);
        const pct = Number(node.element_percent || 0);
        const click =
          level === 'middle'
            ? `selectMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
            : level === 'small'
              ? `selectSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
              : `selectSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
        return `<button class="node${active ? ' active' : ''}" onclick="${click}">
          <span class="node-icon">${esc(node.code)}</span>
          <span class="node-main">
            <span class="node-title">${esc(node.name)}</span>
            <span class="node-sub">요소 API ${pct.toFixed(1)}% · 세분류 ${fmt.format(node.classification_count)} · 단위 ${fmt.format(node.unit_count)}</span>
            ${progressBar(pct)}
          </span>
        </button>`;
      }).join('');
    }
    function renderUnits(units) {
      q('unitMeta').textContent = units.length ? `${fmt.format(units.length)}개 표시` : '';
      if (!units.length) {
        renderEmpty('unitList', selectedCodes().sub ? '능력단위가 없습니다.' : '세분류를 선택하세요.');
        return;
      }
      q('unitList').innerHTML = units.map(unit => {
        const total = Number(unit.element_count || 0);
        const matched = Number(unit.element_matched || 0);
        const pct = total ? matched / total * 100 : 0;
        return `<button class="node" onclick="openUnit('${esc(unit.unit_code)}')">
          <span class="node-icon">${esc(String(unit.unit_level_raw || '-'))}</span>
          <span class="node-main">
            <span class="node-title">${esc(unit.unit_name_refined || unit.unit_name_raw)}</span>
            <span class="node-sub">${esc(unit.unit_code)} · 요소 API ${pct.toFixed(1)}% · ${fmt.format(matched)} / ${fmt.format(total)}</span>
            ${progressBar(pct)}
          </span>
        </button>`;
      }).join('');
    }
    async function loadTaxonomy() {
      const codes = selectedCodes();
      const majors = await api('/api/taxonomy?' + taxonomyParams('major').toString());
      const middles = codes.major ? await api('/api/taxonomy?' + taxonomyParams('middle').toString()) : {nodes:[]};
      const smalls = codes.major && codes.middle ? await api('/api/taxonomy?' + taxonomyParams('small').toString()) : {nodes:[]};
      const subs = codes.major && codes.middle && codes.small ? await api('/api/taxonomy?' + taxonomyParams('sub').toString()) : {nodes:[]};
      const units = codes.major && codes.middle && codes.small && codes.sub
        ? await api('/api/units?' + scopeParams(false).toString())
        : {units:[]};
      renderMajorTiles(majors.nodes);
      renderNodeList('middleList', 'middleMeta', middles.nodes, 'middle');
      renderNodeList('smallList', 'smallMeta', smalls.nodes, 'small');
      renderNodeList('subList', 'subMeta', subs.nodes, 'sub');
      renderUnits(units.units || []);
    }
    async function selectMajor(major) {
      setScope(major, '', '', '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectMiddle(major, middle) {
      setScope(major, middle, '', '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectSmall(major, middle, small) {
      setScope(major, middle, small, '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectSub(major, middle, small, sub) {
      setScope(major, middle, small, sub);
      q('keyword').value = '';
      currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
      clearDetail();
      await refreshAll();
    }
    async function openUnit(unitCode) {
      q('keyword').value = unitCode;
      currentCard = {kind:'element', state:'processed', title:'능력단위요소 전처리 완료'};
      await loadCurrentItems();
      await loadDetail('unit', unitCode);
    }
    function selectedName(target) {
      const el = q(target).querySelector('.node.active .node-title');
      return el ? el.textContent : '';
    }
    function selectedMajorName() {
      const el = q('majorTiles').querySelector('.major-tile.active .tile-title');
      return el ? el.textContent : '';
    }
    function renderSubStatus(phases) {
      const codes = selectedCodes();
      const box = q('subStatus');
      if (!codes.major) {
        box.classList.remove('visible');
        box.innerHTML = '';
        return;
      }
      const path = [
        selectedMajorName(),
        selectedName('middleList'),
        selectedName('smallList'),
        selectedName('subList')
      ].filter(Boolean).join(' > ');
      box.classList.add('visible');
      box.innerHTML = `<h3>${esc(scopeLabel())} 선택 범위 온톨로지 준비 현황</h3>
        <div class="muted" style="margin-bottom:10px;">${esc(path)}</div>
        <div class="status-grid">
          ${phases.map(phase => `<div class="status-cell">
            <b>${esc(phase.name)}</b>
            ${statusPill(phase.status)}
            <div class="tile-meta">${phase.percent.toFixed(1)}% · 남은 작업 ${fmt.format(phase.remaining)}</div>
            ${progressBar(phase.percent)}
          </div>`).join('')}
        </div>
        <div class="quick-actions">
          <button class="link" onclick="selectCard('classification','processed','선택 범위 세분류')">세분류/직무정의</button>
          <button class="link" onclick="selectCard('unit','processed','능력단위 전처리 완료')">능력단위</button>
          <button class="link" onclick="selectCard('element','processed','능력단위요소 전처리 완료')">능력단위요소</button>
          <button class="link" onclick="selectCard('criteria','raw','수행준거 미정제')">수행준거 정제</button>
          <button class="link" onclick="selectCard('ksa','raw','KSA 미정제')">KSA 정제</button>
          <button class="link" onclick="selectCard('element','api_not_collected','요소 API 미수집')">요소 API 미수집</button>
          <button class="link" onclick="selectCard('element','api_problem','요소 API 실패/없음')">요소 API 실패</button>
          <button class="link" onclick="selectCard('quality','open','열린 품질 이슈')">품질 이슈</button>
        </div>`;
    }

    async function refreshAll() {
      await loadTaxonomy();
      await loadStatus();
      await loadProgress();
      await loadWorkbench();
      await loadCurrentItems();
      await loadIssues();
      await loadOntologyStatus();
      await loadOntology();
      await loadRecommendationRuns();
    }

    const conceptTypeLabels = {knowledge:'지식', skill:'기술', attitude:'태도'};
    const conceptStateLabels = {
      definition_missing:'정의 미작성',
      relation_missing:'관계 미연결',
      duplicates:'중복 후보',
      reviewed:'검토 완료'
    };

    async function loadOntologyStatus() {
      if (!hasSelectedScope()) {
        q('ontologyWorkbenchMeta').textContent = '';
        q('ontologyWorkbench').innerHTML = '<div class="muted">대분류를 선택하면 온톨로지 구축 현황이 표시됩니다.</div>';
        return;
      }
      const data = await api('/api/ontology-status?' + scopeParams(false).toString());
      q('ontologyWorkbenchMeta').textContent = `${scopeLabel()} 기준`;
      q('ontologyWorkbench').innerHTML = data.statuses.map(item => `<div class="ontology-stat">
        <h3>${esc(item.label)} (${esc(item.concept_type)})</h3>
        <div>전체 개념 <b>${fmt.format(item.total)}</b></div>
        <div>정의 작성 <b>${fmt.format(item.definition_done)}</b> / ${fmt.format(item.total)}</div>
        <div>관계 연결 <b>${fmt.format(item.relation_done)}</b> / ${fmt.format(item.total)}</div>
        <div>검토 완료 <b>${fmt.format(item.reviewed)}</b></div>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','definition_missing')">정의 미작성 ${fmt.format(item.definition_missing)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','relation_missing')">관계 미연결 ${fmt.format(item.relation_missing)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','duplicates')">중복 후보 ${fmt.format(item.duplicate_like)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','reviewed')">검토 완료 ${fmt.format(item.reviewed)}</button>
      </div>`).join('');
    }

    async function loadConceptWorkItems(conceptType, state) {
      const params = scopeParams(false);
      params.set('concept_type', conceptType);
      params.set('state', state);
      params.set('limit', '100');
      const data = await api('/api/concepts?' + params.toString());
      q('conceptWorkItems').innerHTML = data.concepts.map(item => `<tr>
        <td><b>${esc(item.concept_name)}</b><br><span class="muted">concept_id ${item.concept_id}</span></td>
        <td>${esc(conceptTypeLabels[item.concept_type] || item.concept_type)}</td>
        <td>${item.definition ? '<span class="ok">작성됨</span>' : '<span class="warn">미작성</span>'}<br><span class="muted">${esc((item.definition || '').slice(0, 80))}</span></td>
        <td>${item.relation_count ? '<span class="ok">연결됨</span>' : '<span class="warn">미연결</span>'}<br><span class="muted">${fmt.format(item.relation_count)}개</span></td>
        <td>${fmt.format(item.alias_count)}</td>
        <td>${item.sample_ksa_id ? `<button class="link" onclick="loadDetail('ksa','${esc(item.sample_ksa_id)}')">개념 정의/관계</button>` : '<span class="muted">KSA 연결 없음</span>'}</td>
      </tr>`).join('');
      if (!data.concepts.length) {
        q('conceptWorkItems').innerHTML = `<tr class="guide-row"><td colspan="6">${esc(conceptTypeLabels[conceptType] || conceptType)} · ${esc(conceptStateLabels[state] || state)} 대상이 없습니다.</td></tr>`;
      }
    }

    function renderOntologyItem(kind, id, label, text, status) {
      return `<button class="ontology-item" onclick="loadDetail('${esc(kind)}','${esc(id)}')">
        <b>${esc(label)}</b>
        <span class="muted">${esc(text || '')}</span>
        ${statusPill(status || '')}
      </button>`;
    }

    async function loadOntology() {
      if (!hasSelectedScope()) {
        q('ontologyMeta').textContent = '';
        q('ontologyTree').innerHTML = '<div class="muted">대분류를 선택하면 능력단위-요소-수행준거-KSA 구조가 표시됩니다.</div>';
        return;
      }
      const params = scopeParams(false);
      params.set('limit', hasSelectedSub() ? '50' : '12');
      const data = await api('/api/ontology?' + params.toString());
      q('ontologyMeta').textContent = `${scopeLabel()} · 능력단위 ${fmt.format(data.units.length)} / ${fmt.format(data.total_units)} 표시`;
      if (!data.units.length) {
        q('ontologyTree').innerHTML = '<div class="muted">선택 범위에 능력단위가 없습니다.</div>';
        return;
      }
      q('ontologyTree').innerHTML = data.units.map((unit, idx) => `<details class="ontology-unit" ${idx === 0 ? 'open' : ''}>
        <summary>${esc(unit.unit_code)} ${esc(unit.unit_name)} ${statusPill(unit.api_match_status)}</summary>
        <div class="ontology-body">
          ${unit.elements.map(element => `<div class="ontology-element">
            <h4>${esc(element.element_no)}. ${esc(element.element_name)} ${statusPill(element.api_match_status)}</h4>
            <div class="ontology-columns">
              <div class="ontology-group">
                <h5>수행준거</h5>
                <div class="ontology-list">
                  ${element.criteria.length ? element.criteria.map(item =>
                    renderOntologyItem('criteria', item.criteria_id, `수행준거 ${item.criteria_no}`, item.criteria_text_refined || item.criteria_text_raw, item.review_status)
                  ).join('') : '<div class="muted">수행준거 없음</div>'}
                </div>
              </div>
              <div class="ontology-group">
                <h5>KSA 지식·기술·태도</h5>
                <div class="ontology-list">
                  ${['지식','기술','태도'].map(group => {
                    const rows = element.ksa_groups[group] || [];
                    return `<div>
                      <b>${esc(group)}</b>
                      ${rows.length ? rows.map(item =>
                        renderOntologyItem(
                          'ksa',
                          item.ksa_id,
                          `${item.ksa_type_name} ${item.ksa_no} · ${item.concept_name || item.ksa_text_refined || item.ksa_text_raw}`,
                          item.definition ? `정의: ${item.definition}` : `원문: ${item.ksa_text_raw}`,
                          item.definition_status || item.review_status
                        )
                      ).join('') : '<div class="muted">항목 없음</div>'}
                    </div>`;
                  }).join('')}
                </div>
              </div>
            </div>
          </div>`).join('')}
        </div>
      </details>`).join('');
    }

    async function loadRecommendationRuns() {
      const params = new URLSearchParams();
      params.set('limit', '25');
      const keyword = q('keyword').value.trim();
      if (keyword) params.set('query', keyword);
      const data = await api('/api/recommendation-runs?' + params.toString());
      q('recommendationMeta').textContent = `${fmt.format(data.total)} runs`;
      if (!data.runs.length) {
        q('recommendationRuns').innerHTML = '<tr class="guide-row"><td colspan="5">No saved recommendation runs.</td></tr>';
        q('recommendationDetail').textContent = 'Run recommend_education_for_duty from MCP to create an audit trail.';
        return;
      }
      q('recommendationRuns').innerHTML = data.runs.map(run => {
        const summary = run.summary || {};
        const target = run.target || {};
        return `<tr>
          <td><b>${run.run_id}</b><br><span class="muted">${esc(run.created_at)}</span></td>
          <td>${esc(run.query)}</td>
          <td>${esc(target.duty_name || '')}<br><span class="muted">${esc(target.sqf_job || target.source_key || '')}</span></td>
          <td>modules ${fmt.format(summary.recommended_modules_count || 0)}<br><span class="muted">concepts ${fmt.format(summary.ontology_concepts_used || 0)}</span></td>
          <td><button class="link" onclick="loadRecommendationDetail(${run.run_id})">Evidence</button></td>
        </tr>`;
      }).join('');
      await loadRecommendationDetail(data.runs[0].run_id);
    }

    async function loadRecommendationDetail(runId) {
      const data = await api('/api/recommendation-detail?run_id=' + encodeURIComponent(runId));
      if (data.error) {
        q('recommendationDetail').textContent = data.error;
        return;
      }
      const items = (data.items || []).map(item => {
        const payload = item.payload || {};
        return `#${item.rank} ${payload.learn_module_name || item.learn_module_name || 'NCS-derived objective'} (${item.confidence_grade}, ${Number(item.confidence_score || 0).toFixed(2)})`;
      }).join('\\n');
      const evidence = (data.evidence || []).slice(0, 30).map(ev => {
        return `- ${ev.evidence_type} ${ev.source_table || ''} ${ev.source_id || ''}: ${ev.evidence_summary || ev.evidence_text || ''}`;
      }).join('\\n');
      q('recommendationDetail').textContent = [
        `Run ${data.run.run_id} / ${data.run.created_at}`,
        `Query: ${data.run.query}`,
        '',
        'Items:',
        items || '(none)',
        '',
        'Evidence:',
        evidence || '(none)'
      ].join('\\n');
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
      renderSubStatus(data.phases);
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
      if (!hasSelectedScope() && currentCard.kind !== 'quality') {
        q('listTitle').textContent = '분류 선택 후 전처리 항목 표시';
        q('listMeta').textContent = '대분류를 먼저 선택하세요.';
        q('items').innerHTML = '<tr class="guide-row"><td colspan="5">대분류를 클릭하면 이 영역에 선택 범위의 세분류와 전처리 항목이 표시됩니다.</td></tr>';
        clearDetail('분류와 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
        return;
      }
      const params = scopeParams(true);
      params.set('kind', currentCard.kind);
      params.set('state', currentCard.state);
      const data = await api('/api/items?' + params.toString());
      q('listTitle').textContent = `${currentCard.title} · ${scopeLabel()}`;
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
      const labels = {
        classification: ['세분류명', '세분류명', '직무정의 원문', '직무정의 정제본'],
        unit: ['능력단위명 원문', '능력단위명 정제본', '능력단위 정의 원문', '능력단위 정의 정제본'],
        element: ['능력단위요소명 원문', '능력단위요소명 정제본', 'API 요소명', '요소 설명 정제본'],
        criteria: ['수행준거 번호', '수행준거 번호', '수행준거 원문', '수행준거 정제본'],
        ksa: ['KSA 유형/번호', '대표 개념명', 'KSA 원문', '개념 정의'],
        quality: ['이슈 유형', '이슈 유형', '이슈 내용', '권장 조치']
      }[currentDetail.kind] || ['원문 명칭', '정제 명칭', '원문/정의/내용', '정제 내용'];
      q('titleRawLabel').textContent = labels[0];
      q('titleRefinedLabel').textContent = labels[1];
      q('bodyRawLabel').textContent = labels[2];
      q('bodyRefinedLabel').textContent = labels[3];
      q('detailStatus').innerHTML = [
        statusPill(currentDetail.status || ''),
        currentDetail.api_status ? statusPill(currentDetail.api_status) : '',
        currentDetail.definition_status ? statusPill(`definition:${currentDetail.definition_status}`) : '',
        currentDetail.relation_status ? statusPill(`relation:${currentDetail.relation_status}`) : '',
        currentDetail.body_refined ? '<span class="ok">정의 작성됨</span>' : '<span class="warn">정의 없음</span>'
      ].filter(Boolean).join(' ');
      q('context').textContent = currentDetail.context || '';
      q('titleRaw').textContent = currentDetail.title_raw || '';
      q('bodyRaw').textContent = currentDetail.body_raw || '';
      q('titleRefined').value = currentDetail.title_refined || '';
      q('titleRefined').placeholder = currentDetail.title_raw || '';
      q('bodyRefined').value = currentDetail.body_refined || '';
      q('bodyRefined').placeholder = currentDetail.body_raw || '';
      q('titleEditWrap').style.display = currentDetail.can_refine_title ? 'block' : 'none';
      q('bodyEditWrap').style.display = currentDetail.can_refine_body ? 'block' : 'none';
      q('ontologyConceptFields').style.display = currentDetail.kind === 'ksa' ? 'block' : 'none';
      if (currentDetail.kind === 'ksa') {
        q('conceptAliases').value = (currentDetail.aliases || []).join('\\n');
        q('parentConcepts').value = ((currentDetail.relations || {}).parent || []).join('\\n');
        q('childConcepts').value = ((currentDetail.relations || {}).child || []).join('\\n');
        q('relatedConcepts').value = ((currentDetail.relations || {}).related || []).join('\\n');
        q('relatedCriteria').innerHTML = (currentDetail.related_criteria || []).length
          ? currentDetail.related_criteria.map(item => `<div><b>수행준거 ${esc(item.criteria_no)}</b><br>${esc(item.criteria_text_raw)}</div>`).join('')
          : '<span class="muted">연결된 수행준거가 없습니다.</span>';
      }
    }

    async function saveCurrentDetail() {
      if (!currentDetail) return;
      if (!currentDetail.can_refine_title && !currentDetail.can_refine_body) {
        alert('이 항목은 읽기 전용 근거입니다.');
        return;
      }
      const needsTitle = currentDetail.can_refine_title && !q('titleRefined').value.trim();
      const needsBody = currentDetail.can_refine_body && !q('bodyRefined').value.trim();
      if (needsTitle || needsBody) {
        alert('정제본이 비어 있습니다. 직접 입력하거나 "원문 그대로 사용"을 누른 뒤 저장하세요.');
        return;
      }
      await api('/api/preprocess', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          kind: currentDetail.kind,
          id: currentDetail.id,
          title_refined: q('titleRefined').value,
          body_refined: q('bodyRefined').value,
          aliases: q('conceptAliases').value,
          parent_concepts: q('parentConcepts').value,
          child_concepts: q('childConcepts').value,
          related_concepts: q('relatedConcepts').value
        })
      });
      await loadDetail(currentDetail.kind, currentDetail.id);
      await loadCurrentItems();
      await loadStatus();
      await loadWorkbench();
    }

    function fillRawAsRefined() {
      if (!currentDetail) return;
      if (currentDetail.kind === 'ksa') {
        if (currentDetail.can_refine_title && !q('titleRefined').value.trim()) {
          q('titleRefined').value = currentDetail.body_raw || currentDetail.title_raw || '';
        }
        alert('KSA 개념 정의는 원문을 그대로 복사하지 않습니다. 정의는 직접 작성하세요.');
        return;
      }
      if (currentDetail.can_refine_title && !q('titleRefined').value.trim()) {
        q('titleRefined').value = currentDetail.title_raw || '';
      }
      if (currentDetail.can_refine_body && !q('bodyRefined').value.trim()) {
        q('bodyRefined').value = currentDetail.body_raw || '';
      }
    }

    async function loadIssues() {
      if (!hasSelectedScope()) {
        q('issues').innerHTML = '<tr class="guide-row"><td colspan="6">대분류를 선택하면 이 영역에 선택 범위의 품질 이슈가 표시됩니다.</td></tr>';
        return;
      }
      const params = scopeParams(false);
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

    q('majorTiles').addEventListener('click', (event) => {
      const button = event.target.closest('[data-major-code]');
      if (!button) return;
      selectMajor(button.dataset.majorCode).catch(err => alert(err.message));
    });
    renderMajorTiles(fallbackMajorNodes);
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
            elif parsed.path == "/api/taxonomy":
                self.json_response(get_taxonomy(self.server.db_path, params))
            elif parsed.path == "/api/ontology":
                self.json_response(get_ontology(self.server.db_path, params))
            elif parsed.path == "/api/ontology-status":
                self.json_response(get_ontology_status(self.server.db_path, params))
            elif parsed.path == "/api/concepts":
                self.json_response(get_concepts(self.server.db_path, params))
            elif parsed.path == "/api/recommendation-runs":
                self.json_response(get_recommendation_runs(self.server.db_path, params))
            elif parsed.path == "/api/recommendation-detail":
                self.json_response(get_recommendation_detail(self.server.db_path, params))
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


def prepare_dashboard_db(db_path: Path, *, prepare_ontology: bool = False) -> None:
    path = Path(db_path).resolve()
    if path in _DB_SCHEMA_PREPARED_PATHS and (
        not prepare_ontology or path in _DB_ONTOLOGY_PREPARED_PATHS
    ):
        return
    with _DB_PREPARE_LOCK:
        schema_ready = path in _DB_SCHEMA_PREPARED_PATHS
        ontology_ready = path in _DB_ONTOLOGY_PREPARED_PATHS
        if schema_ready and (not prepare_ontology or ontology_ready):
            return
        conn = connect(path)
        try:
            if not schema_ready:
                initialize_database(conn)
                _DB_SCHEMA_PREPARED_PATHS.add(path)
            if prepare_ontology and not ontology_ready:
                ensure_ontology_seeded(conn)
                _DB_ONTOLOGY_PREPARED_PATHS.add(path)
        finally:
            conn.close()


def connect_db(db_path: Path, *, prepare_ontology: bool = False):
    prepare_dashboard_db(db_path, prepare_ontology=prepare_ontology)
    return connect(db_path)


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


def quality_issue_scope_filter(params: dict[str, list[str]]) -> tuple[str, list[str]]:
    scope_clauses, scope_values = classification_filters(params, "c")
    if not scope_clauses:
        return "", []
    scope_sql = " AND ".join(scope_clauses)
    clause = f"""
    (
      (
        qi.target_type = 'classification'
        AND EXISTS (
          SELECT 1
          FROM classifications c
          WHERE c.classification_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'unit'
        AND EXISTS (
          SELECT 1
          FROM competency_units cu
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE cu.unit_code = qi.target_id
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'element'
        AND EXISTS (
          SELECT 1
          FROM competency_elements ce
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE ce.element_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'criteria'
        AND EXISTS (
          SELECT 1
          FROM performance_criteria pc
          JOIN competency_elements ce ON ce.element_id = pc.element_id
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE pc.criteria_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'ksa'
        AND EXISTS (
          SELECT 1
          FROM ksa_items ki
          JOIN competency_elements ce ON ce.element_id = ki.element_id
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE ki.ksa_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
    )
    """
    values: list[str] = []
    for _ in range(5):
        values.extend(scope_values)
    return clause, values


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
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    open_issue_where = "WHERE qi.resolved_at IS NULL"
    resolved_issue_where = "WHERE qi.resolved_at IS NOT NULL"
    if issue_scope:
        open_issue_where += f" AND {issue_scope}"
        resolved_issue_where += f" AND {issue_scope}"
    open_issues = count_query(
        conn,
        f"SELECT COUNT(*) FROM quality_issues qi {open_issue_where}",
        issue_scope_values,
    )
    resolved_issues = count_query(
        conn,
        f"SELECT COUNT(*) FROM quality_issues qi {resolved_issue_where}",
        issue_scope_values,
    )
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
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    issue_where = "WHERE qi.resolved_at IS NULL"
    if issue_scope:
        issue_where += f" AND {issue_scope}"
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
            "count": count_query(
                conn,
                f"SELECT COUNT(*) FROM quality_issues qi {issue_where}",
                issue_scope_values,
            ),
            "description": "품질 진단에서 발견된 검토 항목",
        },
    ]
    conn.close()
    return {"cards": cards}


def get_taxonomy(db_path: Path, params: dict[str, list[str]]) -> dict:
    level = first(params, "level", "major")
    levels = {
        "major": {
            "code": "c.major_code",
            "name": "c.major_name",
            "select": [
                "c.major_code AS major_code",
                "'' AS middle_code",
                "'' AS small_code",
                "'' AS sub_code",
            ],
            "filters": [],
            "order": "c.major_code",
            "group": "c.major_code, c.major_name",
        },
        "middle": {
            "code": "c.middle_code",
            "name": "c.middle_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "'' AS small_code",
                "'' AS sub_code",
            ],
            "filters": ["major_code"],
            "order": "c.middle_code",
            "group": "c.major_code, c.middle_code, c.middle_name",
        },
        "small": {
            "code": "c.small_code",
            "name": "c.small_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "c.small_code AS small_code",
                "'' AS sub_code",
            ],
            "filters": ["major_code", "middle_code"],
            "order": "c.small_code",
            "group": "c.major_code, c.middle_code, c.small_code, c.small_name",
        },
        "sub": {
            "code": "c.sub_code",
            "name": "c.sub_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "c.small_code AS small_code",
                "c.sub_code AS sub_code",
            ],
            "filters": ["major_code", "middle_code", "small_code"],
            "order": "c.sub_code",
            "group": "c.major_code, c.middle_code, c.small_code, c.sub_code, c.sub_name",
        },
    }
    if level not in levels:
        raise ValueError(f"unsupported taxonomy level: {level}")

    spec = levels[level]
    clauses: list[str] = []
    values: list[str] = []
    for field in spec["filters"]:
        value = first(params, field)
        if value:
            clauses.append(f"c.{field} = ?")
            values.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params, default=500, maximum=1200)
    select_codes = ", ".join(spec["select"])
    conn = connect_db(db_path)
    rows = conn.execute(
        f"""
        SELECT
            {select_codes},
            {spec["code"]} AS code,
            {spec["name"]} AS name,
            COUNT(DISTINCT c.classification_id) AS classification_count,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'matched' THEN ce.element_id END) AS element_matched,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'not_collected' THEN ce.element_id END) AS element_not_collected,
            COUNT(DISTINCT CASE WHEN ce.api_match_status IN ('api_failed', 'no_data') THEN ce.element_id END) AS element_problem
        FROM classifications c
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        {where}
        GROUP BY {spec["group"]}
        ORDER BY {spec["order"]}
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    nodes = []
    for row in rows:
        node = dict(row)
        total = int(node["element_count"] or 0)
        matched = int(node["element_matched"] or 0)
        node["element_percent"] = percent(matched, total) if total else 0
        nodes.append(node)
    conn.close()
    return {"level": level, "nodes": nodes}


def get_ontology(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    unit_limit = safe_limit(params, default=20, maximum=50)
    conn = connect_db(db_path, prepare_ontology=True)
    units = conn.execute(
        f"""
        SELECT
            cu.unit_code,
            COALESCE(cu.unit_name_refined, cu.unit_name_raw) AS unit_name,
            cu.unit_level_raw,
            cu.review_status,
            cu.api_match_status,
            c.major_code, c.middle_code, c.small_code, c.sub_code,
            c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY cu.unit_code
        LIMIT ?
        """,
        [*values, unit_limit],
    ).fetchall()
    result_units = []
    for unit in units:
        elements = conn.execute(
            """
            SELECT
                ce.element_id,
                ce.element_no,
                COALESCE(ce.element_name_refined, ce.element_name_raw) AS element_name,
                ce.review_status,
                ce.api_match_status
            FROM competency_elements ce
            WHERE ce.unit_code = ?
            ORDER BY CAST(ce.element_no AS INTEGER), ce.element_id
            """,
            (unit["unit_code"],),
        ).fetchall()
        result_elements = []
        for element in elements:
            criteria = conn.execute(
                """
                SELECT
                    pc.criteria_id,
                    pc.criteria_no,
                    pc.criteria_text_raw,
                    pc.criteria_text_refined,
                    pc.review_status
                FROM performance_criteria pc
                WHERE pc.element_id = ?
                ORDER BY CAST(pc.criteria_no AS INTEGER), pc.criteria_id
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_rows = conn.execute(
                """
                SELECT
                    ki.ksa_id,
                    ki.ksa_type_code,
                    ki.ksa_type_name,
                    ki.ksa_no,
                    ki.ksa_text_raw,
                    ki.ksa_text_refined,
                    ki.review_status,
                    oc.concept_id,
                    oc.concept_name,
                    oc.definition,
                    oc.definition_status,
                    oc.relation_status,
                    oc.review_status AS concept_review_status
                FROM ksa_items ki
                LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
                LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
                WHERE ki.element_id = ?
                ORDER BY ki.ksa_type_code, CAST(ki.ksa_no AS INTEGER), ki.ksa_id
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_groups: dict[str, list[dict]] = {}
            for row in ksa_rows:
                ksa_groups.setdefault(row["ksa_type_name"], []).append(dict(row))
            result_elements.append(
                {
                    **dict(element),
                    "criteria": [dict(row) for row in criteria],
                    "ksa_groups": ksa_groups,
                }
            )
        result_units.append({**dict(unit), "elements": result_elements})
    total_units = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        """,
        values,
    )
    conn.close()
    return {
        "unit_limit": unit_limit,
        "total_units": total_units,
        "units": result_units,
    }


def concept_scope_filter(params: dict[str, list[str]]) -> tuple[str, list[str]]:
    scope_clauses, scope_values = classification_filters(params, "c")
    if not scope_clauses:
        return "", []
    scope_sql = " AND ".join(scope_clauses)
    clause = f"""
    EXISTS (
      SELECT 1
      FROM ksa_concept_links kcl
      JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
      JOIN competency_elements ce ON ce.element_id = ki.element_id
      JOIN competency_units cu ON cu.unit_code = ce.unit_code
      JOIN classifications c ON c.classification_id = cu.classification_id
      WHERE kcl.concept_id = oc.concept_id
        AND {scope_sql}
    )
    """
    return clause, scope_values


def get_ontology_status(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db(db_path, prepare_ontology=True)
    scope_clause, scope_values = concept_scope_filter(params)
    where_scope = f" AND {scope_clause}" if scope_clause else ""
    types = [
        ("knowledge", "지식"),
        ("skill", "기술"),
        ("attitude", "태도"),
    ]
    statuses = []
    for concept_type, label in types:
        base_values = [concept_type, *scope_values]
        total = count_query(
            conn,
            f"SELECT COUNT(*) FROM ontology_concepts oc WHERE oc.concept_type = ? {where_scope}",
            base_values,
        )
        definition_done = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND oc.definition IS NOT NULL
              AND TRIM(oc.definition) <> ''
              {where_scope}
            """,
            base_values,
        )
        relation_done = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND EXISTS (
                SELECT 1
                FROM ontology_concept_relations rel
                WHERE rel.source_concept_id = oc.concept_id
              )
              {where_scope}
            """,
            base_values,
        )
        reviewed = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND oc.review_status = 'human_reviewed'
              {where_scope}
            """,
            base_values,
        )
        duplicate_like = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND (
                SELECT COUNT(*)
                FROM ontology_concept_aliases alias
                WHERE alias.concept_id = oc.concept_id
              ) > 1
              {where_scope}
            """,
            base_values,
        )
        statuses.append(
            {
                "concept_type": concept_type,
                "label": label,
                "total": total,
                "definition_done": definition_done,
                "definition_missing": max(total - definition_done, 0),
                "relation_done": relation_done,
                "relation_missing": max(total - relation_done, 0),
                "reviewed": reviewed,
                "duplicate_like": duplicate_like,
            }
        )
    conn.close()
    return {"statuses": statuses}


def get_concepts(db_path: Path, params: dict[str, list[str]]) -> dict:
    concept_type = first(params, "concept_type", "knowledge")
    state = first(params, "state", "definition_missing")
    limit = safe_limit(params, default=100, maximum=300)
    clauses = ["oc.concept_type = ?"]
    values: list[str | int] = [concept_type]
    scope_clause, scope_values = concept_scope_filter(params)
    if scope_clause:
        clauses.append(scope_clause)
        values.extend(scope_values)
    if state == "definition_missing":
        clauses.append("(oc.definition IS NULL OR TRIM(oc.definition) = '')")
    elif state == "relation_missing":
        clauses.append(
            """
            NOT EXISTS (
              SELECT 1
              FROM ontology_concept_relations rel
              WHERE rel.source_concept_id = oc.concept_id
            )
            """
        )
    elif state == "reviewed":
        clauses.append("oc.review_status = 'human_reviewed'")
    elif state == "duplicates":
        clauses.append(
            """
            (
              SELECT COUNT(*)
              FROM ontology_concept_aliases alias
              WHERE alias.concept_id = oc.concept_id
            ) > 1
            """
        )
    where = "WHERE " + " AND ".join(clauses)
    conn = connect_db(db_path, prepare_ontology=True)
    rows = conn.execute(
        f"""
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition,
            oc.definition_status,
            oc.relation_status,
            oc.review_status,
            COUNT(DISTINCT alias.alias_id) AS alias_count,
            COUNT(DISTINCT rel.relation_id) AS relation_count,
            MIN(kcl.ksa_id) AS sample_ksa_id
        FROM ontology_concepts oc
        LEFT JOIN ontology_concept_aliases alias ON alias.concept_id = oc.concept_id
        LEFT JOIN ontology_concept_relations rel ON rel.source_concept_id = oc.concept_id
        LEFT JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
        {where}
        GROUP BY oc.concept_id
        ORDER BY oc.review_status, oc.concept_name
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    total = count_query(
        conn,
        f"SELECT COUNT(*) FROM ontology_concepts oc {where}",
        values,
    )
    conn.close()
    return {
        "concept_type": concept_type,
        "state": state,
        "total": total,
        "concepts": [dict(row) for row in rows],
    }


def json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def get_recommendation_runs(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params, default=25, maximum=100)
    clauses: list[str] = []
    values: list[str] = []
    query = first(params, "query")
    target_source_key = first(params, "target_source_key")
    if query:
        clauses.append("query LIKE ?")
        values.append(f"%{query}%")
    if target_source_key:
        clauses.append("target_source_key = ?")
        values.append(target_source_key)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect_db(db_path)
    rows = conn.execute(
        f"""
        SELECT *
        FROM education_recommendation_runs
        {where}
        ORDER BY run_id DESC
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    total = count_query(conn, f"SELECT COUNT(*) FROM education_recommendation_runs {where}", values)
    conn.close()
    runs = []
    for row in rows:
        runs.append(
            {
                "run_id": row["run_id"],
                "query": row["query"],
                "target_source_key": row["target_source_key"],
                "created_at": row["created_at"],
                "request": json_object(row["request_payload"]),
                "target": json_object(row["target_payload"]),
                "summary": json_object(row["summary_payload"]),
                "audit": json_object(row["audit_payload"]),
            }
        )
    return {"total": total, "runs": runs}


def get_recommendation_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    run_id = first(params, "run_id")
    if not run_id:
        raise ValueError("run_id is required")
    conn = connect_db(db_path)
    run = conn.execute(
        "SELECT * FROM education_recommendation_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        conn.close()
        return {"error": "not_found", "run_id": run_id}
    item_rows = conn.execute(
        """
        SELECT *
        FROM education_recommendation_items
        WHERE run_id = ?
        ORDER BY rank
        """,
        (run_id,),
    ).fetchall()
    evidence_rows = conn.execute(
        """
        SELECT *
        FROM education_recommendation_evidence
        WHERE run_id = ?
        ORDER BY item_id, evidence_id
        """,
        (run_id,),
    ).fetchall()
    conn.close()
    return {
        "run": {
            "run_id": run["run_id"],
            "query": run["query"],
            "target_source_key": run["target_source_key"],
            "created_at": run["created_at"],
            "request": json_object(run["request_payload"]),
            "target": json_object(run["target_payload"]),
            "summary": json_object(run["summary_payload"]),
            "audit": json_object(run["audit_payload"]),
        },
        "items": [
            {
                **dict(row),
                "payload": json_object(row["recommendation_payload"]),
            }
            for row in item_rows
        ],
        "evidence": [dict(row) for row in evidence_rows],
    }


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
        issue_scope, issue_scope_values = quality_issue_scope_filter(params)
        issue_where = "WHERE qi.resolved_at IS NULL"
        if issue_scope:
            issue_where += f" AND {issue_scope}"
        rows = conn.execute(
            f"""
            SELECT 'quality' AS kind, qi.issue_id AS id, qi.target_type || ':' || qi.target_id AS code,
                   qi.issue_type AS context, qi.severity AS title, qi.issue_detail AS body,
                   qi.severity AS status, '' AS api_status
            FROM quality_issues qi
            {issue_where}
            ORDER BY CASE qi.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, qi.issue_id
            LIMIT ?
            """,
            [*issue_scope_values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"SELECT COUNT(*) FROM quality_issues qi {issue_where}",
            issue_scope_values,
        )
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
    conn = connect_db(db_path, prepare_ontology=(kind == "ksa"))
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
            SELECT
                ki.*,
                ce.element_name_raw,
                cu.unit_code,
                cu.unit_name_raw,
                c.major_name,
                c.middle_name,
                c.small_name,
                c.sub_name,
                oc.concept_id,
                oc.concept_name,
                oc.definition,
                oc.definition_status,
                oc.relation_status,
                oc.review_status AS concept_review_status
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE ki.ksa_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            aliases = []
            relations = {"parent": [], "child": [], "related": []}
            related_criteria = []
            if row["concept_id"]:
                aliases = [
                    item["alias_text"]
                    for item in conn.execute(
                        """
                        SELECT alias_text
                        FROM ontology_concept_aliases
                        WHERE concept_id = ?
                        ORDER BY alias_text
                        """,
                        (row["concept_id"],),
                    ).fetchall()
                ]
                for rel_type in relations:
                    relations[rel_type] = [
                        item["concept_name"]
                        for item in conn.execute(
                            """
                            SELECT target.concept_name
                            FROM ontology_concept_relations rel
                            JOIN ontology_concepts target ON target.concept_id = rel.target_concept_id
                            WHERE rel.source_concept_id = ? AND rel.relation_type = ?
                            ORDER BY target.concept_name
                            """,
                            (row["concept_id"], rel_type),
                        ).fetchall()
                    ]
                related_criteria = [
                    dict(item)
                    for item in conn.execute(
                        """
                        SELECT DISTINCT pc.criteria_id, pc.criteria_no, pc.criteria_text_raw
                        FROM criteria_concept_links ccl
                        JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
                        WHERE ccl.concept_id = ?
                        ORDER BY CAST(pc.criteria_no AS INTEGER), pc.criteria_id
                        LIMIT 100
                        """,
                        (row["concept_id"],),
                    ).fetchall()
                ]
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}\n{row['element_name_raw']}",
                "title_raw": f"{row['ksa_type_name']} {row['ksa_no']}",
                "title_refined": row["concept_name"] or row["ksa_text_refined"] or row["ksa_text_raw"],
                "body_raw": row["ksa_text_raw"],
                "body_refined": row["definition"] or "",
                "can_refine_title": True,
                "can_refine_body": True,
                "status": row["review_status"],
                "concept_id": row["concept_id"],
                "concept_name": row["concept_name"],
                "concept_type": {"지식": "knowledge", "기술": "skill", "태도": "attitude"}.get(row["ksa_type_name"], "knowledge"),
                "definition_status": row["definition_status"] or "missing",
                "relation_status": row["relation_status"] or "unlinked",
                "concept_review_status": row["concept_review_status"] or "raw",
                "aliases": aliases,
                "relations": relations,
                "related_criteria": related_criteria,
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


def split_lines(value: object) -> list[str]:
    if value is None:
        return []
    raw = str(value).replace(",", "\n").splitlines()
    items: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(item.strip().split())
        key = normalize_concept_key(text)
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def get_or_create_concept(conn, concept_type: str, concept_name: str) -> int:
    name = " ".join(concept_name.strip().split())
    if not name:
        raise ValueError("concept name is required")
    key = normalize_concept_key(name)
    row = conn.execute(
        """
        SELECT concept_id
        FROM ontology_concepts
        WHERE concept_type = ? AND normalized_key = ?
        """,
        (concept_type, key),
    ).fetchone()
    if row:
        return int(row["concept_id"])
    timestamp = now_utc()
    cur = conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type,
            definition_status, relation_status, review_status,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
        """,
        (name, key, concept_type, timestamp, timestamp),
    )
    return int(cur.lastrowid)


def save_concept_aliases(conn, concept_id: int, aliases: list[str]) -> None:
    timestamp = now_utc()
    for alias in aliases:
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concept_aliases(
                concept_id, alias_text, normalized_alias_key, alias_source, created_at
            ) VALUES (?, ?, ?, 'manual', ?)
            """,
            (concept_id, alias, normalize_concept_key(alias), timestamp),
        )


def replace_concept_relations(
    conn,
    *,
    source_concept_id: int,
    concept_type: str,
    relation_type: str,
    target_names: list[str],
) -> None:
    conn.execute(
        """
        DELETE FROM ontology_concept_relations
        WHERE source_concept_id = ? AND relation_type = ?
        """,
        (source_concept_id, relation_type),
    )
    timestamp = now_utc()
    for target_name in target_names:
        target_id = get_or_create_concept(conn, concept_type, target_name)
        if target_id == source_concept_id:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concept_relations(
                source_concept_id, relation_type, target_concept_id,
                relation_label, review_status, created_at
            ) VALUES (?, ?, ?, ?, 'human_reviewed', ?)
            """,
            (source_concept_id, relation_type, target_id, relation_type, timestamp),
        )


def save_manual_preprocess(db_path: Path, payload: dict) -> dict:
    kind = str(payload["kind"])
    item_id = str(payload["id"])
    title_refined = str(payload.get("title_refined", "")).strip()
    body_refined = str(payload.get("body_refined", "")).strip()
    conn = connect_db(db_path, prepare_ontology=(kind == "ksa"))
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
        row = conn.execute(
            """
            SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, kcl.concept_id
            FROM ksa_items ki
            LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            WHERE ki.ksa_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            conn.close()
            raise ValueError(f"unknown ksa id: {item_id}")
        concept_type = {"지식": "knowledge", "기술": "skill", "태도": "attitude"}.get(
            row["ksa_type_name"],
            "knowledge",
        )
        concept_name = title_refined or row["ksa_text_raw"]
        concept_id = get_or_create_concept(conn, concept_type, concept_name)
        timestamp = now_utc()
        conn.execute(
            """
            UPDATE ontology_concepts
            SET concept_name = ?,
                normalized_key = ?,
                definition = ?,
                definition_status = ?,
                review_status = 'human_reviewed',
                updated_at = ?
            WHERE concept_id = ?
            """,
            (
                concept_name,
                normalize_concept_key(concept_name),
                body_refined or None,
                "defined" if body_refined else "missing",
                timestamp,
                concept_id,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
            VALUES (?, ?, 'human_reviewed', ?)
            """,
            (item_id, concept_id, timestamp),
        )
        conn.execute(
            """
            UPDATE ksa_concept_links
            SET concept_id = ?, link_status = 'human_reviewed'
            WHERE ksa_id = ?
            """,
            (concept_id, item_id),
        )
        conn.execute(
            """
            UPDATE ksa_items
            SET ksa_text_refined = ?, review_status = 'human_reviewed'
            WHERE ksa_id = ?
            """,
            (concept_name, item_id),
        )
        aliases = split_lines(payload.get("aliases"))
        aliases.append(row["ksa_text_raw"])
        save_concept_aliases(conn, concept_id, aliases)
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="parent",
            target_names=split_lines(payload.get("parent_concepts")),
        )
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="child",
            target_names=split_lines(payload.get("child_concepts")),
        )
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="related",
            target_names=split_lines(payload.get("related_concepts")),
        )
        relation_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM ontology_concept_relations WHERE source_concept_id = ?",
                (concept_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE ontology_concepts
            SET relation_status = ?, updated_at = ?
            WHERE concept_id = ?
            """,
            ("linked" if relation_count else "unlinked", timestamp, concept_id),
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
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    if issue_scope:
        clauses.append(issue_scope)
        sql_params.extend(issue_scope_values)
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
