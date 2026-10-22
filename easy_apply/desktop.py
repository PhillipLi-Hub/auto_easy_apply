import csv
import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, ttk

from .browser import LinkedInBrowser, NeedsInput, Stopped, DailyLimitReached
from .limits import DailyLimit, cooldown_status
from .limit_report import daily_limit_report, save_limit_report, limit_status, limit_details, today_limit_report
from .models import DATA, Settings, load_settings, save_settings, normalize
from .store import Store, queue_summary, status_label
from .ai import AnswerGenerator, connection_token, effective_api_key
from .usage_ui import UsagePanel
from .cv import read_cv, contact_details
from .questions import validate_question_answer
from .priority import queue_order
from .schedule import ScheduleQueue, schedule_label
from .daily_report import begin_run, finish_run, save_report, build_report, report_days, local_day, summary as daily_summary, application_totals
from .report_mail import ReportOutbox, load_config as load_mail_config, save_config as save_mail_config, verify as verify_mail, safe_error as mail_error
from .dashboard_sync import DashboardPublisher, load_config as load_dashboard_config, valid_origin


class BrowserWorker(threading.Thread):
    def __init__(self, store, messages):
        super().__init__(daemon=True)
        self.store, self.messages = store, messages
        self.commands, self.stop_event = queue.Queue(), threading.Event()
        self.dashboard_runtime = None

    def cooldown(self, seconds):
        self.dashboard_runtime = {'state': 'paused' if seconds else 'running',
                                  'resume_at': (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat() if seconds else None}
        self.messages.put(('cooldown', seconds))

    def log(self, message):
        if message.startswith('Run finished:'):
            self.last_run_result = message
        self.messages.put(("log", message))

    def run(self):
        browser = LinkedInBrowser(self.stop_event, self.log,
                                  cooldown=self.cooldown)
        try:
            while True:
                command = self.commands.get()
                action, settings = command[:2]
                scheduled_day = command[2] if len(command) > 2 else None
                if action == "quit":
                    break
                token = connection_token(settings)
                def connection(state, detail):
                    self.messages.put(('ai_connection', (token, state, detail)))
                browser.ai_connection = connection
                outcome, detail = 'completed', ''
                self.last_run_result = ''
                run_id = None
                try:
                    if action in {'apply', 'scheduled_apply'}:
                        run_id = begin_run(self.store, action, scheduled_day)
                        self.dashboard_runtime = {'state': 'running', 'resume_at': None}
                    if action == "open":
                        browser.open()
                    elif action in {'verify_report_email', 'retry_report_email'}:
                        try:
                            if action == 'verify_report_email':
                                self.messages.put(('email_status', verify_mail(self.store)))
                            for delivery in ReportOutbox(self.store).deliver():
                                self.messages.put(('email_status', delivery))
                        except Exception as error:
                            self.messages.put(('email_status', mail_error(error)))
                    elif action == "test_ai":
                        def check_stop():
                            if self.stop_event.is_set():
                                raise Stopped("Stopped by user")
                        generator = AnswerGenerator(settings, self.log, check_stop, connection=connection)
                        answer = generator.answer({}, "Briefly summarize my relevant professional experience.", "textarea", maxlength=1000)
                        self.log("AI connection test succeeded: " + answer)
                    elif action == 'scheduled_apply':
                        browser.open()
                        # Refresh yesterday's page (including any old limit popup).
                        browser.page.goto(settings.search_url(), wait_until='domcontentloaded')
                        browser.run(settings, self.store)
                    else:
                        if not browser.page or browser.page.is_closed():
                            raise NeedsInput("Open the browser and sign in first")
                        if action == "scan":
                            browser.discover(settings, self.store, full_scan=True)
                        elif action == "apply":
                            browser.run(settings, self.store)
                except (DailyLimitReached, DailyLimit) as exc:
                    outcome, detail = 'daily_limit', str(exc)
                    report = daily_limit_report(self.store, getattr(exc, 'linkedin_message', str(exc)),
                                                getattr(exc, 'confirmed_in_run', None), getattr(exc, 'job_id', None))
                    report['scheduled'] = bool(scheduled_day)
                    try:
                        report['report_path'] = str(save_limit_report(self.store, report))
                    except OSError as error:
                        report['save_error'] = str(error)
                    self.messages.put(('daily_limit', report))
                except (NeedsInput, Stopped) as exc:
                    outcome, detail = 'stopped', str(exc)
                    self.log(str(exc))
                    self.messages.put(("result", str(exc)))
                except Exception as exc:
                    outcome, detail = 'error', f'{type(exc).__name__}: {exc}'
                    self.log(f"{type(exc).__name__}: {exc}")
                    self.messages.put(("result", f"Stopped: {type(exc).__name__}: {exc}"))
                finally:
                    if scheduled_day:
                        ScheduleQueue(self.store).finish(scheduled_day, outcome, detail or self.last_run_result or 'Scheduled run finished')
                        self.log(f'Scheduled run {scheduled_day}: {outcome}. {detail or self.last_run_result}')
                    if run_id is not None:
                        self.dashboard_runtime = {'state': outcome, 'resume_at': None}
                        try:
                            days = finish_run(self.store, run_id, outcome, detail or self.last_run_result)
                            for day in days:
                                report, path = save_report(self.store, day)
                                self.log(f"Daily report {day}: {report['total_confirmed']} confirmed applications. Saved: {path}")
                                self.messages.put(('daily_report', {'date': day, 'path': str(path)}))
                                if outcome == 'daily_limit' and day == days[-1]:
                                    ReportOutbox(self.store).enqueue(report)
                            if outcome == 'daily_limit':
                                for delivery in ReportOutbox(self.store).deliver():
                                    self.messages.put(('email_status', delivery))
                        except Exception as error:
                            self.log(f'Daily report could not be saved: {error}')
                            self.messages.put(('result', f'Run {outcome}; daily report could not be saved: {error}'))
                    self.messages.put(("idle", "Ready"))
        finally:
            try:
                browser.close()
            except Exception:
                pass


class App:
    def __init__(self, root):
        self.root, self.messages = root, queue.Queue()
        root.title("Easy Apply · Personal desktop assistant")
        root.geometry("1120x870")
        root.minsize(1000, 800)
        self.store = Store()
        self.store.recover()
        self.report_outbox = ReportOutbox(self.store)
        self.report_outbox.recover()
        self.schedule = ScheduleQueue(self.store)
        self.schedule.recover()
        try:
            self.settings = load_settings()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Settings need attention", str(exc))
            self.settings = Settings()
        self.store.import_question_notes()
        self.worker = BrowserWorker(self.store, self.messages)
        self.worker.start()
        self.busy = self.closing = False
        self.next_schedule_check = 0
        self.variables, self.action_buttons = {}, []
        self.style()
        self.build()
        self.refresh()
        self.dashboard = DashboardPublisher(self.store, self.messages, lambda: self.worker.dashboard_runtime)
        self.dashboard.start()
        if report := today_limit_report(self.store):
            self.run_result = limit_status(report)
            self.status.set(self.run_result)
        if self.question_groups:
            self.tabs.select(self.needs_panel)
        self.log(self.queue_summary.get())
        if previous := self.schedule.latest():
            self.log(f"Last scheduled run ({previous['day']}): {previous['status']}. {previous['result']}")
        root.after(200, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def style(self):
        style = ttk.Style()
        style.theme_use("clam")
        self.root.configure(bg="#101827")
        style.configure(".", font=("Segoe UI", 10), background="#101827", foreground="#e5eaf3")
        style.configure("TFrame", background="#101827")
        style.configure("TLabel", background="#101827")
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
        style.configure("Count.TLabel", font=("Segoe UI", 13, "bold"), foreground="#5eead4")
        style.configure("Muted.TLabel", foreground="#a6b5cd")
        style.configure("TButton", background="#25354c", padding=(12, 8))
        style.map("TButton", background=[("active", "#36516f"), ("disabled", "#192333")])
        style.configure("Accent.TButton", background="#0e766e", font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#0f9286"), ("disabled", "#192333")])
        style.configure("TEntry", fieldbackground="#1c2a40", foreground="#f8fafc", insertcolor="white", padding=6)
        style.configure("TNotebook", background="#101827", borderwidth=0)
        style.configure("TNotebook.Tab", background="#1c2a40", padding=(8, 9))
        style.map("TNotebook.Tab", background=[("selected", "#31465f")])
        style.configure("Treeview", background="#152237", fieldbackground="#152237", foreground="#e5eaf3", rowheight=30)
        style.configure("Treeview.Heading", background="#25354c", foreground="#e5eaf3", padding=8)
        style.map("Treeview", background=[("selected", "#235a75")])
        style.configure("TCheckbutton", background="#101827")

    def build(self):
        shell = ttk.Frame(self.root, padding=24)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Easy Apply", style="Title.TLabel").pack(anchor="w")
        ttk.Label(shell, text="Your profile. Your filters. A visible browser and a recorded application history.", style="Muted.TLabel").pack(anchor="w", pady=(4, 16))
        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", pady=(0, 16))
        for title, command, accent in [
            ("Save settings", self.save, False),
            ("1  Open LinkedIn / sign in", lambda: self.command("open"), False),
            ("2  Collect jobs to database", lambda: self.command("scan"), False),
            ("3  Start auto apply", lambda: self.command("apply"), True),
        ]:
            button = ttk.Button(toolbar, text=title, command=command, style="Accent.TButton" if accent else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.action_buttons.append(button)
        ttk.Button(toolbar, text="Stop", command=self.stop).pack(side="right")
        self.application_count = tk.StringVar()
        self.next_count_refresh = 0
        ttk.Label(shell, textvariable=self.application_count, style="Count.TLabel", wraplength=1000).pack(anchor="w", pady=(0, 10))
        self.status = tk.StringVar(value="Ready · Save your profile and filters to begin")
        ttk.Label(shell, textvariable=self.status, style="Muted.TLabel", wraplength=1000).pack(anchor="w", pady=(0, 10))
        self.ai_status = tk.StringVar()
        self.ai_connection_result = None
        ttk.Label(shell, textvariable=self.ai_status, style="Muted.TLabel", wraplength=1000).pack(anchor='w', pady=(0, 8))
        if label := schedule_label(DATA):
            ttk.Label(shell, text=label, style='Muted.TLabel').pack(anchor='w', pady=(0, 8))
        tabs = ttk.Notebook(shell)
        tabs.pack(fill="both", expand=True)
        profile, filters, needs, answers, ai, jobs, reports, usage, activity = [ttk.Frame(tabs, padding=18) for _ in range(9)]
        self.tabs, self.needs_panel = tabs, needs
        self.reports_panel = reports
        for panel, name in zip([profile, filters, needs, answers, ai, jobs, reports, usage, activity], ["Profile", "Job filters", "Needs answers", "Saved answers", "AI answers", "Application queue", "Daily reports", "LLM usage", "Activity"]):
            tabs.add(panel, text=name)
        self.usage_panel = usage
        self.llm_usage = UsagePanel(usage, self.store.path.parent)
        self.next_usage_refresh = 0
        self.build_daily_reports(reports)
        self.build_question_inbox(needs)
        for row, (key, title) in enumerate([("first_name", "First name"), ("last_name", "Last name"), ("email", "Email"), ("phone", "Phone number"), ("resume", "Local resume / CV reference"), ("address", "Street address"), ("city", "City of residence"), ("postcode", "Postcode"), ("country", "Country")]):
            self.entry(profile, row, key, title)
        ttk.Button(profile, text="Choose resume…", command=self.choose_resume).grid(row=4, column=2, padx=(8, 0))
        self.resume_source = tk.StringVar(value=self.settings.resume_source)
        ttk.Radiobutton(profile, text="Use first resume already uploaded to LinkedIn", variable=self.resume_source, value="linkedin_first").grid(row=9, column=1, sticky="w", pady=4)
        ttk.Radiobutton(profile, text="Upload the local resume file", variable=self.resume_source, value="local_file").grid(row=10, column=1, sticky="w", pady=4)
        for row, (key, title) in enumerate([
            ("keywords", "LinkedIn search keywords"), ("location", "Location text (e.g. London)"),
            ("title_include", "Title includes ANY phrase"), ("title_exclude", "Title excludes ANY phrase"),
            ("company_exclude", "Exclude companies"), ("description_require", "Description includes ALL phrases"),
            ("max_applications", "Maximum submissions per run"), ("max_results", "New jobs per automatic queue refill")
        ]):
            self.entry(filters, row, key, title)
        self.remote = tk.BooleanVar(value=self.settings.remote_only)
        ttk.Checkbutton(filters, text="Remote only (must be confirmed on the job page)", variable=self.remote).grid(row=8, column=1, sticky="w", pady=8)
        self.allow_missing = tk.BooleanVar(value=self.settings.allow_missing_metadata)
        ttk.Checkbutton(filters, text="Continue when job metadata is missing (known mismatches still skip)", variable=self.allow_missing).grid(row=9, column=0, columnspan=2, sticky="w", pady=4)
        self.prioritize_quant = tk.BooleanVar(value=self.settings.prioritize_quant)
        self.newest_first = tk.BooleanVar(value=self.settings.newest_first)
        self.exclude_sc_required = tk.BooleanVar(value=self.settings.exclude_sc_required)
        self.permanent_full_time_only = tk.BooleanVar(value=self.settings.permanent_full_time_only)
        self.exclude_senior_roles = tk.BooleanVar(value=self.settings.exclude_senior_roles)
        self.run_until_daily_limit = tk.BooleanVar(value=self.settings.run_until_daily_limit)
        ttk.Checkbutton(filters, text='Prioritize quant / quantitative roles', variable=self.prioritize_quant).grid(row=10, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Checkbutton(filters, text='Prioritize last 24 hours; search and apply older jobs afterwards', variable=self.newest_first).grid(row=11, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Checkbutton(filters, text='Exclude jobs that require SC clearance', variable=self.exclude_sc_required).grid(row=12, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Checkbutton(filters, text='Full-time permanent only (exclude contract, temporary, part-time and day rates)', variable=self.permanent_full_time_only).grid(row=13, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Checkbutton(filters, text='Exclude senior and leadership roles (head, lead, director, manager, principal, staff, chief, VP)', variable=self.exclude_senior_roles).grid(row=14, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Checkbutton(filters, text='Continue until LinkedIn daily limit or no eligible jobs (ignore submission cap)', variable=self.run_until_daily_limit).grid(row=15, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Label(answers, text="Saved answers take priority over AI. Use exact question/option wording, especially for eligibility and consent.", wraplength=920).pack(anchor="w")
        self.no_clearance = tk.BooleanVar(value=self.settings.no_security_clearance)
        ttk.Checkbutton(answers, text='I hold no security clearance (answer No to current SC/DV/CTC clearance questions)', variable=self.no_clearance).pack(anchor='w', pady=6)
        self.relocate_anywhere = tk.BooleanVar(value=self.settings.willing_relocate_anywhere)
        ttk.Checkbutton(answers, text='I am willing to relocate anywhere', variable=self.relocate_anywhere).pack(anchor='w', pady=6)
        ttk.Label(answers, text='Example format: {"Phone country code": "United Kingdom (+44)"}\nOnly save answers that are correct for you. Work authorization, salary, experience and consent have no default answers.', style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(6, 12))
        self.answers = self.textbox(answers)
        self.answers.pack(fill="both", expand=True)
        self.answers.insert("1.0", json.dumps(self.settings.answers, indent=2))
        self.ai_enabled = tk.BooleanVar(value=self.settings.ai_enabled)
        ttk.Checkbutton(ai, text="Generate and fill unanswered questions from my CV", variable=self.ai_enabled).pack(anchor="w")
        self.ai_provider = tk.StringVar(value=self.settings.ai_provider)
        provider_row = ttk.Frame(ai)
        provider_row.pack(fill='x', pady=6)
        ttk.Radiobutton(provider_row, text='Local Ollama — no API charges', variable=self.ai_provider, value='ollama').pack(side='left')
        ttk.Radiobutton(provider_row, text='OpenAI — paid API', variable=self.ai_provider, value='openai').pack(side='left', padx=20)
        connection = ttk.Frame(ai)
        connection.pack(fill="x", pady=8)
        self.entry(connection, 0, "ai_model", "OpenAI model")
        self.entry(connection, 1, "max_ai_calls", "Maximum AI calls per run")
        self.entry(connection, 3, "local_model", "Local Ollama model")
        ttk.Label(connection, text="API key (session only)").grid(row=2, column=0, sticky="w", padx=(0, 20))
        self.api_key = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        ttk.Entry(connection, textvariable=self.api_key, show="•").grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(ai, textvariable=self.ai_status, wraplength=920).pack(anchor='w', pady=4)
        ttk.Label(ai, text='A key entered here lasts until you close the app. Enter it again after restarting, or launch with OPENAI_API_KEY set.', style='Muted.TLabel', wraplength=920).pack(anchor='w', pady=4)
        for variable in (self.api_key, self.variables['ai_model'], self.variables['local_model'], self.ai_enabled, self.ai_provider):
            variable.trace_add('write', self.update_ai_status)
        self.update_ai_status()
        buttons = ttk.Frame(ai)
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="Import CV PDF / text", command=self.import_cv).pack(side="left")
        test_button = ttk.Button(buttons, text="Test AI connection", command=lambda: self.command("test_ai"))
        test_button.pack(side="left", padx=8)
        ttk.Button(buttons, text='View LLM usage', command=lambda: self.tabs.select(self.usage_panel)).pack(side='left')
        self.action_buttons.append(test_button)
        ttk.Label(ai, text="Local Ollama processes CV and job text on this PC. It never falls back to OpenAI.\nSelecting OpenAI sends this context to its paid API. Unknown personal facts still need your answer.", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(0, 6))
        self.unknown_experience_as_no = tk.BooleanVar(value=self.settings.unknown_experience_as_no)
        ttk.Checkbutton(ai, text='Use No for unsupported experience/qualification questions (0 for years)',
                        variable=self.unknown_experience_as_no).pack(anchor='w', pady=4)
        self.cv_text = self.textbox(ai, height=8)
        self.cv_text.pack(fill="both", expand=True)
        self.cv_text.insert("1.0", self.settings.cv_text)
        add_row = ttk.Frame(jobs)
        add_row.pack(fill="x", pady=(0, 10))
        self.url = tk.StringVar()
        ttk.Entry(add_row, textvariable=self.url).pack(side="left", fill="x", expand=True)
        ttk.Button(add_row, text="Add job URL", command=self.add_job).pack(side="left", padx=8)
        columns = ("title", "company", "status", "note")
        table = ttk.Frame(jobs)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for name, width in zip(columns, [230, 150, 150, 380]):
            self.tree.heading(name, text=name.title())
            self.tree.column(name, width=width, minwidth=80)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self.select_job)
        self.note = tk.StringVar(value="Select a job to read its full status.")
        ttk.Label(jobs, textvariable=self.note, wraplength=960, style="Muted.TLabel").pack(anchor="w", pady=8)
        self.queue_summary = tk.StringVar()
        ttk.Label(jobs, textvariable=self.queue_summary, style="Muted.TLabel", wraplength=960).pack(anchor="w", pady=4)
        actions = ttk.Frame(jobs)
        actions.pack(fill="x")
        ttk.Button(actions, text="Requeue after fixing answers", command=self.requeue).pack(side="left")
        ttk.Button(actions, text="Mark applied after manual verification", command=self.mark_applied).pack(side="left", padx=8)
        ttk.Button(actions, text="Export CSV", command=self.export).pack(side="right")
        self.activity = self.textbox(activity, state="disabled")
        self.activity.pack(fill="both", expand=True)
        if self.store.rows('queued'):
            tabs.select(jobs)
        ttk.Label(shell, text="Automatic submissions start only when you press Start auto apply. LinkedIn prohibits third-party automation and may restrict accounts.", style="Muted.TLabel", wraplength=1000).pack(anchor="w", pady=(12, 0))

    def entry(self, panel, row, key, title):
        ttk.Label(panel, text=title).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 20))
        variable = tk.StringVar(value=str(getattr(self.settings, key)))
        self.variables[key] = variable
        ttk.Entry(panel, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)
        panel.columnconfigure(1, weight=1)

    def build_question_inbox(self, panel):
        self.question_groups, self.editing_question = {}, None
        self.question_summary = tk.StringVar(value='No unanswered questions recorded.')
        ttk.Label(panel, textvariable=self.question_summary, wraplength=920).pack(anchor='w', pady=(0, 6))
        ttk.Label(panel, text='Most frequent first; each job counts once. Select a question and save your answer.\nSave while auto apply is stopped. Answers are reused automatically on future applications.',
                  style='Muted.TLabel', wraplength=920).pack(anchor='w', pady=(0, 8))
        # Reserve space for the editor before the expandable question list,
        # keeping Save accessible on smaller desktop windows.
        editor = ttk.Frame(panel)
        editor.pack(side='bottom', fill='x')
        table = ttk.Frame(panel)
        table.pack(fill='both', expand=True)
        self.question_tree = ttk.Treeview(table, columns=('count', 'question'), show='headings', selectmode='browse', height=5)
        self.question_tree.heading('count', text='Jobs')
        self.question_tree.heading('question', text='Question needing an answer')
        self.question_tree.column('count', width=65, stretch=False)
        self.question_tree.column('question', width=770)
        self.question_tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.question_tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.question_tree.configure(yscrollcommand=scrollbar.set)
        self.question_tree.bind('<<TreeviewSelect>>', self.select_question)
        self.question_detail = tk.StringVar(value='Select a question above.')
        details = ttk.Frame(editor)
        details.pack(fill='x', pady=8)
        detail_text = self.textbox(details, height=3, state='disabled')
        detail_text.pack(side='left', fill='x', expand=True)
        detail_scroll = ttk.Scrollbar(details, orient='vertical', command=detail_text.yview)
        detail_scroll.pack(side='right', fill='y')
        detail_text.configure(yscrollcommand=detail_scroll.set)
        def show_detail(*_):
            detail_text.configure(state='normal')
            detail_text.delete('1.0', 'end')
            detail_text.insert('1.0', self.question_detail.get())
            detail_text.configure(state='disabled')
        self.question_detail.trace_add('write', show_detail)
        show_detail()
        ttk.Label(editor, text='Your answer (use the exact option wording if options are shown)').pack(anchor='w')
        self.question_answer = self.textbox(editor, height=2)
        self.question_answer.pack(fill='x', pady=6)
        self.question_save = ttk.Button(editor, text='Save answer and requeue ready jobs', command=self.save_question_answer, style='Accent.TButton')
        self.question_save.pack(anchor='w')
        self.action_buttons.append(self.question_save)

    def refresh_questions(self):
        groups = {item['key']: item for item in self.store.unanswered_questions(self.settings)}
        if groups == self.question_groups:
            return
        selected = self.question_tree.selection()
        self.question_groups = groups
        self.question_tree.delete(*self.question_tree.get_children())
        for key, group in groups.items():
            self.question_tree.insert('', 'end', iid=key, values=(group['count'], ' '.join(group['label'].split())))
        count_jobs = len({job['id'] for group in groups.values() for job in group['jobs']})
        self.question_summary.set(f'{len(groups)} unanswered questions across {count_jobs} blocked jobs.' if groups else 'All recorded questions have answers. New unanswered questions will appear here.')
        if selected and selected[0] in groups:
            self.question_tree.selection_set(selected[0])
        elif self.editing_question not in groups:
            self.editing_question = None
            self.question_detail.set('Select a question above.' if groups else 'No unanswered questions remain in this list.')
            self.question_answer.delete('1.0', 'end')

    def select_question(self, event=None):
        selected = self.question_tree.selection()
        if not selected or selected[0] == self.editing_question:
            return
        group = self.question_groups.get(selected[0])
        if not group:
            return
        self.editing_question = selected[0]
        options = list(dict.fromkeys(' / '.join(spec['options']) for spec in group['specs'] if spec.get('options')))
        constraints = list(dict.fromkeys(
            f"{spec.get('kind', 'text')}" + (f", max {spec['maxlength']} characters" if spec.get('maxlength') else '')
            for spec in group['specs']))
        employers = ', '.join(dict.fromkeys(job['company'] or job['title'] or job['id'] for job in group['jobs']))
        self.question_detail.set(group['label'] + '\n' + ('Options: ' + '; '.join(options) if options else 'Field: ' + '; '.join(constraints)) + '\nJobs: ' + employers[:300])
        self.question_answer.delete('1.0', 'end')
        self.question_answer.insert('1.0', self.settings.answer(group['label']) or '')

    def save_question_answer(self):
        if self.busy:
            self.status.set('Stop auto apply before saving an answer; it will be used on the next run.')
            return
        selected = self.question_tree.selection()
        group = self.question_groups.get(selected[0]) if selected else None
        if not group:
            messagebox.showinfo('Select a question', 'Select a question in Needs answers first.')
            return
        try:
            answer = self.question_answer.get('1.0', 'end').strip()
            for spec in group['specs']:
                validate_question_answer(answer, spec)
            candidate = self.collect()
            candidate.answers = {question: value for question, value in candidate.answers.items()
                                 if normalize(question) != group['key']}
            candidate.answers[group['label']] = answer
            save_settings(candidate)
            self.settings = candidate
            self.answers.delete('1.0', 'end')
            self.answers.insert('1.0', json.dumps(candidate.answers, indent=2, ensure_ascii=False))
            count = self.store.resolve_question(group['key'], candidate)
            self.editing_question = None
            self.question_answer.delete('1.0', 'end')
            self.refresh()
            message = f'Answer saved for future applications. {count} blocked jobs returned to the queue.'
            self.status.set(message)
            self.log(message)
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror('Check answer', str(exc))

    def update_ai_status(self, *_):
        settings = Settings(api_key=self.api_key.get(), ai_model=self.variables['ai_model'].get().strip(),
                            ai_provider=self.ai_provider.get(), local_model=self.variables['local_model'].get().strip())
        local = settings.ai_provider == 'ollama'
        provider = 'Local Ollama' if local else 'OpenAI'
        token = connection_token(settings)
        if self.ai_connection_result and self.ai_connection_result[0] != token:
            self.ai_connection_result = None
        if not local and not effective_api_key(settings):
            text = 'OpenAI: not connected — enter an API key in AI answers. Contact fields still fill locally.'
        elif self.ai_connection_result:
            _, state, detail = self.ai_connection_result
            label = {'testing': 'request in progress', 'verified': 'connection verified', 'failed': 'last request failed'}[state]
            text = f'{provider}: {label} — {detail}'
        elif local:
            text = f'Local Ollama: {settings.local_model} — connection not tested. No API key or API charges.'
        else:
            text = 'OpenAI: key configured, connection not tested — click Test AI connection.'
        if not self.ai_enabled.get():
            text = 'AI answers disabled. ' + text
        self.ai_status.set(text)

    @staticmethod
    def textbox(parent, **kwargs):
        return tk.Text(parent, bg="#152237", fg="#e5eaf3", insertbackground="white", relief="flat", padx=12, pady=12, font=("Consolas", 10), wrap="word", **kwargs)

    def collect(self):
        values = {key: var.get().strip() for key, var in self.variables.items()}
        for key in ["max_applications", "max_results", "max_ai_calls"]:
            values[key] = int(values[key])
        values["remote_only"] = self.remote.get()
        values['prioritize_quant'] = self.prioritize_quant.get()
        values['newest_first'] = self.newest_first.get()
        values['exclude_sc_required'] = self.exclude_sc_required.get()
        values['permanent_full_time_only'] = self.permanent_full_time_only.get()
        values['exclude_senior_roles'] = self.exclude_senior_roles.get()
        values['run_until_daily_limit'] = self.run_until_daily_limit.get()
        values["answers"] = json.loads(self.answers.get("1.0", "end"))
        values.update(resume_source=self.resume_source.get(), allow_missing_metadata=self.allow_missing.get(),
                      ai_enabled=self.ai_enabled.get(), ai_model=self.variables["ai_model"].get().strip(),
                      unknown_experience_as_no=self.unknown_experience_as_no.get(),
                      ai_provider=self.ai_provider.get(), no_security_clearance=self.no_clearance.get(),
                      willing_relocate_anywhere=self.relocate_anywhere.get(),
                      cv_text=self.cv_text.get("1.0", "end").strip(), api_key=self.api_key.get().strip())
        return Settings(**values).validate()

    def save(self):
        try:
            self.settings = self.collect()
            save_settings(self.settings)
            excluded = self.store.exclude_ineligible_jobs(self.settings)
            if excluded:
                self.log(f'Removed {excluded} jobs excluded by role level, clearance or employment preferences from pending applications.')
            self.log("Settings saved locally.")
            self.refresh()
            return True
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror("Check settings", str(exc))
            return False

    def choose_resume(self):
        path = filedialog.askopenfilename(filetypes=[("Resume", "*.pdf *.doc *.docx")])
        if path:
            self.variables["resume"].set(path)

    def import_cv(self):
        path = filedialog.askopenfilename(filetypes=[("CV context", "*.pdf *.txt")])
        if not path:
            return
        try:
            text = read_cv(path)
            self.cv_text.delete("1.0", "end")
            self.cv_text.insert("1.0", text)
            for key, value in contact_details(text).items():
                if not self.variables[key].get().strip():
                    self.variables[key].set(value)
            if path.lower().endswith(".pdf"):
                self.variables["resume"].set(path)
            self.log("CV imported locally. Check the extracted text and profile, then Save settings.")
        except Exception as exc:
            messagebox.showerror("Could not import CV", str(exc))

    def command(self, action):
        if self.busy or not self.save():
            return
        try:
            if action in {"scan", "apply"}:
                self.settings.validate(for_run=True)
        except ValueError as exc:
            messagebox.showerror("Cannot start", str(exc))
            return
        self.busy = True
        self.worker.stop_event.clear()
        self.status.set({"open": "Opening browser…", "scan": "Collecting search results into the database…", "test_ai": "Testing the selected AI provider with your CV…", "apply": "Auto apply running — Stop ends the run after the current browser operation"}[action])
        for button in self.action_buttons:
            button.state(["disabled"])
        self.worker.commands.put((action, self.settings))

    def stop(self):
        self.worker.stop_event.set()
        self.log("Stop requested. Any submit already sent must still be verified.")

    def build_daily_reports(self, panel):
        controls = ttk.Frame(panel)
        controls.pack(fill='x', pady=(0, 12))
        ttk.Label(controls, text='Report date (London):').pack(side='left')
        self.report_day = tk.StringVar(value=local_day())
        self.report_dates = ttk.Combobox(controls, textvariable=self.report_day, state='readonly', width=14)
        self.report_dates.pack(side='left', padx=8)
        self.report_dates.bind('<<ComboboxSelected>>', lambda event: self.refresh_daily_report())
        ttk.Button(controls, text='Refresh / save report', command=lambda: self.refresh_daily_report(generate=True)).pack(side='left', padx=4)
        ttk.Button(controls, text='Open HTML report', command=self.open_daily_report).pack(side='left', padx=4)
        web_controls = ttk.Frame(panel)
        web_controls.pack(fill='x', pady=(0, 12))
        ttk.Button(web_controls, text='Open web dashboard', command=self.open_dashboard).pack(side='left', padx=(0, 8))
        ttk.Button(web_controls, text='Sync dashboard now', command=lambda: self.dashboard.request_sync()).pack(side='left', padx=(0, 8))
        self.dashboard_status = tk.StringVar(value='Dashboard sync enabled' if load_dashboard_config(self.store).get('enabled') else 'Web dashboard not connected')
        ttk.Label(web_controls, textvariable=self.dashboard_status, wraplength=650).pack(side='left', fill='x', expand=True)
        mail_controls = ttk.Frame(panel)
        mail_controls.pack(fill='x', pady=(0, 12))
        ttk.Button(mail_controls, text='Gmail setup', command=self.setup_report_email).pack(side='left', padx=(0, 8))
        ttk.Button(mail_controls, text='Retry pending email', command=lambda: self.mail_action('retry_report_email')).pack(side='left', padx=(0, 8))
        config = load_mail_config(self.store)
        initial = self.report_outbox.latest_status()
        if not config.get('enabled'):
            initial = 'Email disabled. Use Gmail setup to enable daily-limit reports.'
        elif not (self.store.path.parent / 'gmail_report_secret.bin').exists():
            initial = 'Gmail setup required: save an App Password to enable delivery.'
        self.email_status = tk.StringVar(value=initial)
        ttk.Label(mail_controls, textvariable=self.email_status, wraplength=650).pack(side='left', fill='x', expand=True)
        self.report_text = self.textbox(panel, state='disabled')
        self.report_text.pack(fill='both', expand=True)
        self.refresh_daily_report()

    def refresh_daily_report(self, generate=False):
        self.report_dates['values'] = report_days(self.store)
        day = self.report_day.get() or local_day()
        if generate and not self.busy:
            report, _ = save_report(self.store, day)
        else:
            report = build_report(self.store, day)
        lines = [daily_summary(report), '', report['counting_rule'], '', 'Confirmed roles:']
        lines.extend(f"{row['category']} | {row['title'] or row['job_id']} | {row['company']} | {row['local_time']}" for row in report['applications'])
        self.report_text.configure(state='normal')
        self.report_text.delete('1.0', 'end')
        self.report_text.insert('1.0', '\n'.join(lines))
        self.report_text.configure(state='disabled')

    def open_dashboard(self):
        import webbrowser
        config = load_dashboard_config(self.store)
        if config.get('origin'):
            webbrowser.open(valid_origin(config['origin']))
        else:
            self.dashboard_status.set('Connect a private dashboard first; see README setup instructions.')

    def open_daily_report(self):
        if self.busy:
            return
        import webbrowser
        _, path = save_report(self.store, self.report_day.get())
        webbrowser.open(path.resolve().as_uri())

    def setup_report_email(self):
        if self.busy:
            return
        config = load_mail_config(self.store)
        window = tk.Toplevel(self.root)
        window.title('Gmail report delivery')
        panel = ttk.Frame(window, padding=20)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Email the daily report to your own Gmail when LinkedIn’s daily limit is reached.', wraplength=560).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        address = tk.StringVar(value=config.get('address', ''))
        password = tk.StringVar()
        enabled = tk.BooleanVar(value=config.get('enabled', True))
        ttk.Label(panel, text='Gmail address').grid(row=1, column=0, sticky='w', pady=6)
        ttk.Entry(panel, textvariable=address, width=45).grid(row=1, column=1, pady=6)
        ttk.Label(panel, text='Google App Password').grid(row=2, column=0, sticky='w', pady=6)
        ttk.Entry(panel, textvariable=password, show='*', width=45).grid(row=2, column=1, pady=6)
        ttk.Label(panel, text='Leave blank to keep the saved password. Stored encrypted for this Windows account.\nUse a Google App Password, not your normal Gmail password.', wraplength=560).grid(row=3, column=0, columnspan=2, sticky='w', pady=8)
        ttk.Checkbutton(panel, text='Email automatically on the daily limit', variable=enabled).grid(row=4, column=0, columnspan=2, sticky='w')
        def open_password_page():
            import webbrowser
            webbrowser.open('https://myaccount.google.com/apppasswords')
        ttk.Button(panel, text='Create a Google App Password', command=open_password_page).grid(row=5, column=0, pady=12, sticky='w')
        def save():
            try:
                save_mail_config(self.store, address.get(), enabled.get(), password.get())
            except (ValueError, OSError) as error:
                messagebox.showerror('Gmail setup', str(error), parent=window)
                return
            password.set('')
            window.destroy()
            if enabled.get():
                self.mail_action('verify_report_email')
            else:
                self.email_status.set('Automatic email disabled. Local reports remain available.')
        ttk.Button(panel, text='Save and verify', command=save).grid(row=5, column=1, pady=12, sticky='e')

    def mail_action(self, action):
        if self.busy:
            return
        self.busy = True
        self.email_status.set('Checking Gmail report delivery…')
        for button in self.action_buttons:
            button.state(['disabled'])
        self.worker.commands.put((action, self.settings))

    def check_schedule(self):
        if self.closing:
            return
        if time.monotonic() < self.next_schedule_check:
            return
        self.next_schedule_check = time.monotonic() + 2
        day = self.schedule.claim(busy=self.busy)
        if not day:
            return
        if self.busy:
            self.log('Daily scheduled start skipped: another app operation is active.')
            return
        try:
            settings = load_settings().validate(for_run=True)
        except (ValueError, TypeError, OSError) as exc:
            self.schedule.finish(day, 'error', f'Check saved settings: {exc}')
            self.status.set(f'Scheduled start stopped: check saved settings. {exc}')
            self.log(self.status.get())
            return
        self.busy = True
        self.worker.stop_event.clear()
        self.run_result = 'Scheduled run finished. Check Activity and application history.'
        self.status.set(f'Daily scheduled auto apply starting ({day})…')
        self.log('Daily schedule is starting auto apply with the saved profile and job filters.')
        for button in self.action_buttons:
            button.state(['disabled'])
        self.worker.commands.put(('scheduled_apply', settings, day))

    def show_daily_limit_notice(self, details):
        # Non-modal: an unattended notice must not block tomorrow's scheduler.
        previous = getattr(self, 'limit_notice', None)
        if previous is not None and previous.winfo_exists():
            previous.destroy()
        notice = self.limit_notice = tk.Toplevel(self.root)
        notice.title('LinkedIn daily limit reached')
        notice.transient(self.root)
        panel = ttk.Frame(notice, padding=20)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=details, wraplength=650, justify='left').pack(fill='x')
        ttk.Button(panel, text='OK', command=notice.destroy).pack(anchor='e', pady=(15, 0))

    def add_job(self):
        if self.busy:
            return
        try:
            added = self.store.add(self.url.get().strip())
            self.log("Job added." if added else "Job already exists; previous status preserved.")
            self.url.set("")
            self.refresh()
        except ValueError as exc:
            messagebox.showerror("Invalid job URL", str(exc))

    def selected(self):
        selected = self.tree.selection()
        return selected[0] if selected else None

    def requeue(self):
        if self.busy:
            return
        if self.selected() and not self.store.requeue(self.selected()):
            messagebox.showinfo("Status preserved", "Only skipped, error or needs-input jobs can be requeued. Applied and unverified submissions are protected from automatic retries.")
        self.refresh()

    def mark_applied(self):
        if self.busy or not self.selected():
            return
        self.store.update(self.selected(), "applied", "User marked applied after manual verification", source='manual_verified')
        self.refresh()

    def select_job(self, event=None):
        selected = self.selected()
        row = next((r for r in self.store.rows() if r["id"] == selected), None)
        if row:
            self.note.set(f"{row['url']}\n{status_label(row)}: {row['note']}")

    def refresh_application_count(self):
        today, total = application_totals(self.store)
        self.application_count.set(f'Confirmed applications by this app — Today (London): {today}  |  All time: {total}')
        self.next_count_refresh = time.monotonic() + 5

    def refresh(self):
        self.refresh_application_count()
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        rows = self.store.rows()
        if self.settings.prioritize_quant or self.settings.newest_first:
            rows.sort(key=lambda row: (row['status'] != 'queued',
                       queue_order(row, self.settings) if row['status'] == 'queued' else (0, 0, 0)))
        self.queue_summary.set(queue_summary(rows, self.settings))
        self.refresh_questions()
        for row in rows:
            self.tree.insert("", "end", iid=row["id"], values=(row["title"] or row["id"], row["company"], status_label(row), row["note"]))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        fields = ["id", "url", "title", "company", "status", "application_source", "note", "updated"]
        with open(path, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in self.store.rows():
                # Prevent spreadsheet formula execution from employer-controlled text.
                writer.writerow({key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value) for key, value in row.items()})
        self.log("Application history exported.")

    def log(self, text):
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "idle":
                    self.busy = False
                    self.dashboard.request_sync()
                    self.status.set(getattr(self, "run_result", "Ready · Check Activity and the application queue"))
                    self.run_result = "Ready · Check Activity and the application queue"
                    for button in self.action_buttons:
                        button.state(["!disabled"])
                    self.refresh()
                elif kind == "result":
                    self.run_result = value
                elif kind == 'cooldown':
                    self.status.set(cooldown_status(value))
                elif kind == 'daily_limit':
                    self.run_result = limit_status(value)
                    self.status.set(self.run_result)
                    details = limit_details(value)
                    self.log(details)
                    if not self.closing:
                        self.show_daily_limit_notice(details)
                elif kind == 'daily_report':
                    self.report_day.set(value['date'])
                    self.refresh_daily_report()
                    if not self.closing:
                        self.tabs.select(self.reports_panel)
                elif kind == 'dashboard_status':
                    self.dashboard_status.set(value)
                elif kind == 'email_status':
                    self.email_status.set(value)
                    self.log(value)
                elif kind == 'ai_connection':
                    self.ai_connection_result = value
                    self.update_ai_status()
                else:
                    self.log(value)
                    if value.startswith(("Run finished:", "Configured run limit reached", "Search finished:")):
                        self.run_result = value
                    if self.busy:
                        self.status.set(value[:180])
                        self.refresh()
        except queue.Empty:
            pass
        if self.closing and not self.worker.is_alive():
            self.root.destroy()
            return
        self.check_schedule()
        if time.monotonic() >= self.next_count_refresh:
            self.refresh_application_count()
        if self.tabs.select() == str(self.usage_panel) and time.monotonic() >= self.next_usage_refresh:
            self.llm_usage.refresh()
            self.next_usage_refresh = time.monotonic() + 2
        self.root.after(200, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.dashboard.close()
        self.worker.stop_event.set()
        self.worker.commands.put(("quit", None))
        self.status.set("Closing browser and saving application state…")
        for button in self.action_buttons:
            button.state(["disabled"])


def main(quiet_if_open=False):
    # A second instance must not share the browser profile or recover a live send.
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    lock = (DATA / "app.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    root = tk.Tk()
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        root.withdraw()
        if not quiet_if_open:
            messagebox.showinfo("Already open", "The Easy Apply desktop app is already running.")
        root.destroy()
        lock.close()
        return
    try:
        app = App(root)
        import sys
        if '--email-setup' in sys.argv:
            app.tabs.select(app.reports_panel)
            root.after(300, app.setup_report_email)
        if '--llm-usage' in sys.argv:
            app.tabs.select(app.usage_panel)
        root.mainloop()
    finally:
        lock.close()
