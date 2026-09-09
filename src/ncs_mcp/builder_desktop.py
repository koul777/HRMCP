"""Tk desktop UI. Long operations run off the Tk thread; only verified versions deploy."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .config import PROJECT_ROOT, load_settings
from .data_builder import BuilderError, DataBuilder, inspect_workbook
from .builder_progress import describe_progress, workflow_percent
from .builder_discovery import discover_project
from .builder_session import BuilderSession


class BuilderCancelled(BaseException):
    """Unwind collectors without treating cancellation as an API retry error."""


class BuilderWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.closing = False
        self.cancel_requested = threading.Event()
        self.active_phase = None
        self.started_at = None
        self.operation_failed = False
        self.phase_states = {number: "pending" for number in range(1, 5)}
        self.phase_labels = {number: tk.StringVar(value=f"{number}단계 · 대기") for number in range(1, 5)}
        self.engine = DataBuilder(progress=self.report_progress)
        self.session = BuilderSession(self.engine.state)
        self.last_journal_at = 0.0
        self.selected_version: str | None = None
        self.source = tk.StringVar()
        self.baseline = tk.StringVar(value=str(self.engine.current_db()))
        self.deploy_root = tk.StringVar(value=str(PROJECT_ROOT / "deploy/vercel_mcp_app"))
        self.production_url = tk.StringVar()
        self.project_label = tk.StringVar(value="연결 설정을 찾지 못했습니다. 기존 MCP 프로젝트 폴더를 찾아보기로 선택하세요.")
        self.detect_project()
        settings = load_settings()
        self.training = tk.BooleanVar(value=bool(settings.training_course_service_key))
        self.job_base = tk.BooleanVar(value=bool(settings.job_base_service_key))
        self.full_snapshot = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="새 Excel을 선택하면 변경분 검토부터 시작할 수 있습니다.")
        self._buttons = []
        root.title("NCS DB 업데이트 빌더 · 원본 변경에서 온톨로지·MCP 반영까지")
        root.geometry("1160x900")
        root.minsize(950, 700)
        root.configure(bg="#eef2f7")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#eef2f7")
        style.configure("TLabel", background="#eef2f7", font=("맑은 고딕", 10))
        style.configure("TButton", font=("맑은 고딕", 10), padding=(12, 9))
        style.configure("TNotebook.Tab", padding=(18, 9), font=("맑은 고딕", 10))
        style.configure("Title.TLabel", font=("맑은 고딕", 23, "bold"), foreground="#122646")
        style.configure("Accent.TButton", background="#185adb", foreground="white")
        outer = ttk.Frame(root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="NCS DB 업데이트 빌더", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="원본 변경분 검토  →  온톨로지 갱신  →  경량 DB 검증  →  Vercel MCP 업데이트").pack(anchor="w", pady=(4, 18))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        source_tab, api_tab, release_tab, deploy_tab, history_tab = [ttk.Frame(tabs, padding=20) for _ in range(5)]
        for frame, title in zip((source_tab, api_tab, release_tab, deploy_tab, history_tab), ("① 원본 · 온톨로지", "② API 갱신", "③ 경량 DB 생성", "④ Vercel 반영", "버전 · 실행 기록")):
            tabs.add(frame, text=title)
        for number, frame in enumerate((source_tab, api_tab, release_tab, deploy_tab), 1):
            ttk.Label(frame, textvariable=self.phase_labels[number], wraplength=1020).pack(anchor="w", pady=(0, 8))
        self._path_row(source_tab, "새 NCS 정보망 Excel (.xlsx)", self.source, self.choose_source)
        self._path_row(source_tab, "비교 기준 DB (최초에는 현재 로컬 원본 DB)", self.baseline, self.choose_baseline)
        ttk.Label(source_tab, text="전체 파일을 읽어 변경 여부를 확인하고, 변경된 능력단위를 갱신합니다.\n원본·이전 DB는 보존합니다. 연결된 관계의 재계산 범위는 검토 보고서에 표시합니다.", wraplength=950).pack(anchor="w", pady=10)
        row = ttk.Frame(source_tab)
        row.pack(fill="x", pady=10)
        self.button(row, "Excel 구조 확인", self.preview).pack(side="left", padx=(0, 8))
        self.button(row, "변경분 검토 · 온톨로지 만들기", self.build, accent=True).pack(side="left")
        ttk.Label(source_tab, text="1단계만 실행합니다. 완료 후 ② API 갱신을 별도로 실행할 수 있습니다.").pack(anchor="w", pady=6)
        ttk.Checkbutton(source_tab, text="전체 원본 파일입니다. 빠진 능력단위는 새 버전에서 제외 대상으로 검토합니다.", variable=self.full_snapshot).pack(anchor="w", pady=3)
        ttk.Label(source_tab, text="자동 검토는 데이터·연결 품질 검사입니다. 사람의 정의 승인이나 의미 검토를 대신하지 않습니다.", wraplength=950).pack(anchor="w", pady=8)
        self.preview_text = ScrolledText(source_tab, height=8, font=("맑은 고딕", 10), relief="flat", padx=10, pady=10)
        self.preview_text.pack(fill="both", expand=True)
        ttk.Label(api_tab, text="전체 NCS 범위로 수집하고 응답 완결성·원문 보존·연결 상태를 검사합니다.").pack(anchor="w", pady=(0, 15))
        for title, variable, present in (("훈련과정 API", self.training, bool(settings.training_course_service_key)),
                                        ("직업기초능력 API", self.job_base, bool(settings.job_base_service_key))):
            ttk.Checkbutton(api_tab, text=f"{title}  ·  키 {'설정됨' if present else '미설정'}", variable=variable).pack(anchor="w", pady=8)
        self.button(api_tab, "선택 API 점검 · 갱신", self.refresh_api, accent=True).pack(anchor="w", pady=15)
        self.button(api_tab, "중단한 작업 이어하기", self.resume_selected).pack(anchor="w", pady=3)
        ttk.Label(api_tab, text="선택 버전이 있으면 그 버전을, 없으면 비교 기준 DB를 사용합니다.\n수집 실패나 응답 누락이 있으면 배포 가능 버전을 만들지 않습니다.\nAPI에서 보이지 않는 행을 자동 삭제하지 않습니다.", wraplength=940).pack(anchor="w", pady=10)
        ttk.Separator(api_tab).pack(fill="x", pady=14)
        ttk.Label(api_tab, text=f"자격 API · 키 {'설정됨' if settings.qualification_service_key else '미설정'}\n자격·NCS006은 재시도 제한과 운영자 실행 조건이 있어 자동 일괄 수집에서 제외됩니다.\n해당 수집의 상태·실행 가능 여부는 기존 운영 대시보드에서 확인합니다.", wraplength=940).pack(anchor="w")
        self.button(api_tab, "API 운영 보고서 폴더", lambda: self.open_folder(PROJECT_ROOT / "reports")).pack(anchor="w", pady=12)
        self._path_row(release_tab, "Vercel 연결 프로젝트 폴더", self.deploy_root, self.choose_deploy_root)
        ttk.Label(release_tab, textvariable=self.project_label, wraplength=950).pack(anchor="w", pady=8)
        self.button(release_tab, "기존 연결 자동 찾기", self.detect_project).pack(anchor="w")
        self._path_row(deploy_tab, "배포 대상 프로젝트 (③과 동일)", self.deploy_root, self.choose_deploy_root)
        ttk.Label(deploy_tab, text="운영 MCP URL").pack(anchor="w", pady=(12, 3))
        ttk.Entry(deploy_tab, textvariable=self.production_url).pack(fill="x")
        row = ttk.Frame(release_tab)
        row.pack(fill="x", pady=20)
        self.button(row, "경량 DB 만들기 · 검증", self.package, accent=True).pack(side="left", padx=(0, 8))
        self.button(row, "프로젝트 확인", self.show_project).pack(side="left")
        self.button(deploy_tab, "④ Vercel MCP 업데이트", self.deploy, accent=True).pack(anchor="w", pady=16)
        ttk.Label(release_tab, text="검증된 경량 패키지만 별도 배포 폴더에 넣습니다.\n원본 Excel과 대용량 로컬 DB는 Vercel에 업로드하지 않습니다.\n임시 배포 검증 → 운영 도메인 전환 → 운영 MCP 검증 순서로 진행합니다.\n운영 검증까지 성공한 버전이 다음 변경분 비교의 기준이 됩니다.", wraplength=930).pack(anchor="w", pady=12)
        self.selection_label = ttk.Label(release_tab, text="선택된 버전 없음", wraplength=900)
        self.selection_label.pack(anchor="w", pady=10)
        self.deploy_selection = ttk.Label(deploy_tab, text="선택된 버전 없음", wraplength=900)
        self.deploy_selection.pack(anchor="w", pady=10)
        ttk.Label(deploy_tab, text="③에서 만든 패키지를 검증용으로 배포한 뒤, 연결 검증을 통과하면 운영 주소에 반영합니다.\n각 단계는 버튼을 눌러 실행하며 자동으로 다음 단계가 시작되지 않습니다.", wraplength=950).pack(anchor="w", pady=10)
        self.history = ttk.Treeview(history_tab, columns=("version", "kind", "status"), show="headings", height=8)
        for column, title, width in (("version", "버전", 390), ("kind", "작업", 160), ("status", "결과", 140)):
            self.history.heading(column, text=title)
            self.history.column(column, width=width)
        self.history.pack(fill="both", expand=True)
        self.history.bind("<<TreeviewSelect>>", self.select_history)
        row = ttk.Frame(history_tab)
        row.pack(fill="x", pady=8)
        self.button(row, "새로고침", self.reload_history).pack(side="left")
        self.button(row, "선택 버전 보고서 열기", self.open_version).pack(side="left", padx=8)
        self.button(row, "선택 작업 이어하기", self.resume_selected, accent=True).pack(side="left")
        ttk.Label(outer, textvariable=self.status, wraplength=1080).pack(anchor="w", pady=(12, 5))
        self.bar = ttk.Progressbar(outer, mode="indeterminate")
        self.bar.pack(fill="x")
        self.elapsed = tk.StringVar(value="경과시간 00:00")
        ttk.Label(outer, textvariable=self.elapsed).pack(anchor="w")
        self.overall_label = tk.StringVar(value="전체 진행률 · 0% (0/4단계 완료) · 시간 기준이 아닌 완료 단계 기준")
        ttk.Label(outer, textvariable=self.overall_label).pack(anchor="w", pady=(6, 3))
        self.overall_bar = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.overall_bar.pack(fill="x")
        self.log = ScrolledText(outer, height=6, font=("맑은 고딕", 9), relief="flat", padx=8, pady=8)
        self.log.pack(fill="x", pady=(8, 0))
        self.reload_history()
        for attempt in self.session.data['attempts'][-20:]:
            self.log.insert('end', f"{attempt['phase']}단계 · {attempt['status']} · {attempt.get('started_at', '')}\n")
        saved_version = self.session.data.get('selected_version')
        resumable = next((item['version'] for item in self.engine.versions() if self.engine.resume_kind(item['version'])), None)
        if resumable:
            saved_version = resumable
        if saved_version and self.history.exists(saved_version):
            self.history.selection_set(saved_version)
            self.select_history()
            self.status.set("중단 작업을 찾았습니다. '중단한 작업 이어하기'를 누르면 저장된 완료 지점부터 진행합니다." if resumable else "이전 작업 버전을 불러왔습니다. 완료 표시를 확인하고 남은 단계 버튼을 누르세요.")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.poll)

    def button(self, parent, text, callback, accent=False):
        button = ttk.Button(parent, text=text, command=callback, style="Accent.TButton" if accent else "TButton")
        self._buttons.append(button)
        return button

    def _path_row(self, parent, title, variable, choose):
        ttk.Label(parent, text=title).pack(anchor="w", pady=(8, 4))
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.button(row, "찾아보기", choose).pack(side="right")

    def choose_source(self):
        path = filedialog.askopenfilename(title="업데이트된 NCS 원본 선택", filetypes=[("NCS Excel", "*.xlsx")])
        if path:
            self.source.set(path)
            self.full_snapshot.set(False)
            self.preview()

    def choose_baseline(self):
        path = filedialog.askopenfilename(title="비교 기준 DB", filetypes=[("SQLite", "*.db")])
        if path:
            self.baseline.set(path)

    def choose_deploy_root(self):
        path = filedialog.askdirectory(title=".vercel/project.json이 있는 프로젝트 폴더")
        if path:
            self.deploy_root.set(path)
            try:
                from .builder_release import project_configuration
                config = project_configuration(Path(path))
                self.production_url.set(config["production_mcp_url"])
                self.project_label.set(f"선택된 연결: {config['projectName']} · ③ 생성 후 ④에서 운영 반영")
            except (ValueError, OSError, RuntimeError):
                self.production_url.set("")
                self.project_label.set("유효한 Vercel 연결이 없습니다. 기존 MCP 프로젝트 폴더를 선택하세요.")

    def detect_project(self):
        config = discover_project(PROJECT_ROOT, self.engine.state)
        if config:
            self.deploy_root.set(config['deploy_root'])
            self.production_url.set(config['production_mcp_url'])
            self.project_label.set(f"자동 확인: {config['projectName']} · 폴더와 운영 주소가 입력되었습니다.")
        else:
            self.project_label.set("기존 연결을 찾지 못했습니다. 찾아보기로 MCP 프로젝트 폴더를 선택하세요.")

    def selected_sources(self):
        return [name for name, enabled in (("training-courses", self.training.get()), ("job-base", self.job_base.get())) if enabled]

    def report_progress(self, value):
        if self.cancel_requested.is_set():
            raise BuilderCancelled()
        self.events.put(('progress', value))

    def start(self, title, operation, phase=None):
        if self.busy:
            return
        if (self.engine.state / 'operation.lock').exists():
            messagebox.showinfo('작업 실행 확인', '기존 Builder 작업 잠금이 있습니다. 실행 중인 작업의 완료 여부를 먼저 확인하세요.')
            return
        if phase:
            self.session.start(phase, self.selected_version)
        self.busy = True
        self.cancel_requested.clear()
        self.active_phase = phase
        self.started_at = time.monotonic()
        self.operation_failed = False
        if phase:
            self.phase_states[phase] = "running"
            self.phase_labels[phase].set(f"{phase}단계 · 실행 중")
            self.update_overall()
        self.status.set(title)
        for button in self._buttons:
            button.state(["disabled"])
        self.bar.stop()
        self.bar.configure(mode="indeterminate", value=0)
        self.bar.start(12)
        def run():
            try:
                self.events.put(("result", operation()))
            except BuilderCancelled:
                self.events.put(('cancelled', '사용자 종료 요청으로 중단했습니다. 미완료 산출물은 다음 단계에 사용하지 않습니다.'))
            except Exception as exc:
                message = str(exc) if isinstance(exc, BuilderError) else f"{type(exc).__name__}: 실행을 완료하지 못했습니다. 버전 보고서를 확인하세요."
                self.events.put(("error", message))
            finally:
                self.events.put(("done", None))
        threading.Thread(target=run, daemon=False).start()

    def preview(self):
        path = Path(self.source.get())
        self.start("Excel 구조를 확인하고 있습니다.", lambda: {"preview": inspect_workbook(path)})

    def build(self):
        if not self.full_snapshot.get():
            messagebox.showinfo("전체 원본 확인", "전체 파일 여부를 체크하세요. 일부 시트만 올리면 빠진 능력단위가 제외 대상으로 계산됩니다.")
            return
        source, baseline = Path(self.source.get()), Path(self.baseline.get())
        def operation():
            return self.engine.build_delta(source, baseline)
        self.start("업로드된 원본의 변경분과 영향을 검사합니다.", operation, phase=1)

    def refresh_api(self):
        if self.selected_version and self.engine.resume_kind(self.selected_version):
            self.resume_selected()
            return
        sources, version, baseline = self.selected_sources(), self.selected_version, Path(self.baseline.get())
        if not sources:
            messagebox.showinfo("API 선택", "갱신할 API를 선택하세요.")
            return
        def operation():
            source = self.engine.candidate(version) if version else baseline
            return self.engine.refresh_api(source, sources)
        self.start("API를 자동 점검·갱신합니다. 전체 수집에는 시간이 걸릴 수 있습니다.", operation, phase=2)

    def resume_selected(self):
        version = self._require_version()
        if not version:
            return
        kind = self.engine.resume_kind(version)
        if not kind:
            messagebox.showinfo('이어하기', '이 버전에 재사용 가능한 중간 완료 기록이 없습니다. 버전 목록에서 중단 작업을 선택하세요.')
            return
        self.start('저장된 완료 기록을 확인하고 남은 작업을 이어갑니다.',
                   lambda: self.engine.resume(version), phase=1 if kind.startswith('excel') else 2)

    def _require_version(self):
        if not self.selected_version:
            messagebox.showinfo("버전 선택", "먼저 변경분을 빌드하거나 버전 목록에서 검증된 버전을 선택하세요.")
            return None
        return self.selected_version

    def package(self):
        version = self._require_version()
        deploy_root = Path(self.deploy_root.get())
        if version:
            self.start("온톨로지를 포함한 경량 DB와 ZIP을 생성·검증합니다.", lambda: self.engine.package(version, deploy_root), phase=3)

    def show_project(self):
        try:
            project = json.loads((Path(self.deploy_root.get()) / ".vercel/project.json").read_text(encoding="utf-8"))
            messagebox.showinfo("배포 대상", f"프로젝트: {project.get('projectName')}\n프로젝트 ID: {project.get('projectId')}\n운영 URL: {self.production_url.get()}")
        except (ValueError, OSError):
            messagebox.showerror("프로젝트 미연결", "선택 폴더에 유효한 .vercel/project.json이 없습니다.")

    def deploy(self):
        version = self._require_version()
        if not version:
            return
        folder, url = Path(self.deploy_root.get()), self.production_url.get().strip()
        try:
            project = json.loads((folder / ".vercel/project.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            messagebox.showerror("프로젝트 미연결", "배포 프로젝트를 먼저 확인하세요.")
            return
        if not messagebox.askyesno("Vercel MCP 업데이트", f"프로젝트: {project.get('projectName')}\n운영 URL: {url}\n버전: {version}\n\n검증된 패키지를 배포하고 운영 MCP를 업데이트할까요?"):
            return
        self.start("Vercel 배포·MCP 검증을 진행합니다.", lambda: self.engine.deploy(version, folder, url), phase=4)

    def reload_history(self):
        self.history.delete(*self.history.get_children())
        for item in self.engine.versions():
            self.history.insert("", "end", iid=item["version"], values=(item["version"], item["kind"], item["status"]))

    def select_history(self, _event=None):
        if self.busy:
            return
        selection = self.history.selection()
        if selection:
            self.selected_version = selection[0]
            self.selection_label.configure(text=f"선택 버전: {self.selected_version}")
            self.deploy_selection.configure(text=f"선택 버전: {self.selected_version}")
            self.restore_phase_states(self.selected_version)

    def restore_phase_states(self, version):
        folder = self.engine._version_dir(version)
        report = json.loads((folder / "build.json").read_text(encoding="utf-8"))
        release_file = folder / "release.json"
        release = json.loads(release_file.read_text(encoding="utf-8")) if release_file.exists() else {}
        ready = report.get("status") == "ready"
        complete = {1: ready and bool(report.get("source_delta")),
                    2: ready and bool(report.get("sources")),
                    3: release.get('package_validated') is True or release.get("status") in {"package_ready", "deployed"},
                    4: release.get("status") == "deployed" and release.get("ok") is True}
        for number in range(1, 5):
            self.phase_states[number] = "done" if complete[number] else "pending"
            self.phase_labels[number].set(f"{number}단계 · {'완료 (선택 버전)' if complete[number] else '대기'}")
        self.update_overall()

    def update_overall(self):
        percent, count = workflow_percent(self.phase_states)
        self.overall_bar.configure(value=percent)
        self.overall_label.set(f"전체 진행률 · {percent:.0f}% ({count}/4단계 완료) · 완료 단계 기준")

    def open_version(self):
        if self.selected_version:
            self.open_folder(self.engine._version_dir(self.selected_version))

    def open_folder(self, folder):
        if folder.is_dir():
            os.startfile(str(folder))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    if self.active_phase and time.monotonic() - self.last_journal_at >= 2:
                        self.session.progress(value if isinstance(value, dict) else {'stage': value})
                        self.last_journal_at = time.monotonic()
                    description, percent = describe_progress(value)
                    self.status.set("현재 작업: " + description)
                    if self.active_phase:
                        self.phase_labels[self.active_phase].set(description)
                    self.bar.stop()
                    if percent is None:
                        self.bar.configure(mode="indeterminate", value=0)
                        self.bar.start(12)
                    else:
                        self.bar.configure(mode="determinate", value=percent)
                    # Keep frequent byte/page updates out of the persistent visible log.
                    if isinstance(value, str) or value.get("completed") == 0:
                        self.log.insert("end", description + "\n")
                elif kind == "result":
                    text = format_result(value)
                    self.preview_text.delete("1.0", "end")
                    self.preview_text.insert("end", text[:30000])
                    if value.get("version"):
                        self.selected_version = value["version"]
                        self.selection_label.configure(text=f"선택 버전: {self.selected_version}")
                        self.deploy_selection.configure(text=f"선택 버전: {self.selected_version}")
                        self.restore_phase_states(self.selected_version)
                    if self.active_phase:
                        self.session.finish(self.selected_version)
                        self.phase_states[self.active_phase] = "done"
                        self.phase_labels[self.active_phase].set(f"{self.active_phase}단계 · 완료 100%")
                        self.update_overall()
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=100)
                    self.status.set("완료 · 결과와 버전 보고서를 확인하세요.")
                    self.log.insert("end", "작업 완료\n")
                elif kind in {"error", "cancelled"}:
                    self.session.fail(value)
                    self.operation_failed = True
                    if self.active_phase:
                        self.phase_states[self.active_phase] = "failed"
                        self.phase_labels[self.active_phase].set(f"{self.active_phase}단계 · 실패 (미완료)")
                        self.update_overall()
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=0)
                    self.status.set(value)
                    self.log.insert("end", value + "\n")
                    if not self.closing and kind == 'error':
                        messagebox.showerror("작업을 완료하지 못했습니다", value)
                elif kind == "done":
                    self.busy = False
                    self.bar.stop()
                    for button in self._buttons:
                        button.state(["!disabled"])
                    self.reload_history()
                    if self.closing:
                        self.root.destroy()
                        return
                self.log.see("end")
        except queue.Empty:
            pass
        if self.busy and self.started_at is not None:
            seconds = int(time.monotonic() - self.started_at)
            self.elapsed.set(f"경과시간 {seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02} · 현재 작업의 처리량 기준 진행률")
        self.root.after(150, self.poll)

    def close(self):
        if self.busy:
            if self.closing:
                return
            deploying = self.active_phase == 4
            text = ("창을 닫고 진행 중인 배포·운영 검증을 백그라운드에서 마무리할까요?\n운영 전환 도중에는 강제로 중단하지 않습니다."
                    if deploying else "작업 중단을 요청하고 창을 닫을까요?\n진행 중인 DB 처리·API 응답이 안전한 중단 지점에 도달하면 종료합니다.\n완료한 이전 버전은 보존되며 정리 중에는 새 작업을 시작할 수 없습니다.")
            if not messagebox.askyesno('빌더 종료', text):
                return
            self.closing = True
            if not deploying:
                self.cancel_requested.set()
            self.root.withdraw()
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    BuilderWindow(root)
    root.mainloop()


def format_result(result: dict) -> str:
    if "preview" in result:
        preview = result["preview"]
        lines = [f"파일: {preview['filename']}", f"크기: {preview['bytes'] / 1024**2:.1f} MB",
                 f"NCS 시트: {len(preview['sheets'])}개", "", "시트별 예상 데이터 행"]
        lines.extend(f"  {sheet['name']}  ·  {sheet['estimated_rows']:,}행" for sheet in preview['sheets'])
        return "\n".join(lines + ["", preview["note"]])
    lines = [f"버전: {result.get('version', '-')}"]
    delta = result.get("source_delta") or {}
    if delta.get("counts"):
        lines.append("\n능력단위 변경분")
        for key, label in (("inserted", "추가"), ("updated", "변경"), ("deleted", "제외"), ("unchanged", "유지")):
            lines.append(f"  {label}: {delta['counts'].get(key, 0):,}개")
    labels = {"competency_units": "능력단위", "competency_elements": "능력단위요소",
              "performance_criteria": "수행준거", "ksa_items": "원문 KSA", "ontology_concepts": "온톨로지 개념",
              "ncs_training_courses": "훈련과정", "ncs_qualification_items": "자격", "ncs_job_base_competencies": "직업기초능력"}
    if result.get("counts"):
        lines.append("\n갱신 후 데이터")
        lines.extend(f"  {label}: {result['counts'].get(key, 0):,}건" for key, label in labels.items())
    if result.get("ontology_processing") == "incremental_nodes_global_dependent_relations":
        lines.append("\n변경된 원천을 갱신했습니다. 과업 간 유사도와 교육 연결 등 공통 관계는 전체 정합성을 위해 재계산했습니다.")
    if "package" in result:
        lines.append("\n경량 DB · 온톨로지 패키지 검증 완료. Vercel MCP 업데이트를 실행할 수 있습니다.")
    elif "deployment" in result:
        lines.append("\nVercel 운영 MCP 업데이트 및 연결 검증 완료.")
    else:
        lines.append("\n작업 완료. 상세 근거는 버전별 보고서에 저장했습니다.")
    return "\n".join(lines)
