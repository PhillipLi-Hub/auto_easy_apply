"""Read-only desktop view of local LLM request history."""
import csv
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .llm_usage import UsageHistory, LONDON


class UsagePanel:
    def __init__(self, panel, directory):
        self.history = UsageHistory(directory / 'llm_usage.sqlite3')
        self.history.import_legacy(directory / 'ai_answers.jsonl')
        self.history.recover()
        self.day = tk.StringVar(value='All dates')
        self.summary = tk.StringVar()
        self.records = {}
        self.signature = None
        controls = ttk.Frame(panel)
        controls.pack(fill='x')
        ttk.Label(controls, text='Date (London)').pack(side='left')
        self.dates = ttk.Combobox(controls, textvariable=self.day, state='readonly', width=15)
        self.dates.pack(side='left', padx=8)
        self.dates.bind('<<ComboboxSelected>>', lambda _: self.refresh(force=True))
        ttk.Button(controls, text='Refresh', command=lambda: self.refresh(force=True)).pack(side='left')
        ttk.Button(controls, text='Export CSV', command=self.export).pack(side='right')
        ttk.Label(panel, textvariable=self.summary, wraplength=1000).pack(anchor='w', pady=6)
        ttk.Label(panel, text='Select a request to read its full question and response. Accepted means validated, not submitted.\n'
                  'Saved answers and cache hits make no new request. Historical logs contain accepted answers only, not full usage.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=(0, 8))
        table = ttk.Frame(panel)
        table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('time', 'model', 'kind', 'state', 'question'), show='headings', height=7)
        for name, label, width in [('time', 'Time (London)', 155), ('model', 'Model', 160), ('kind', 'Request', 110),
                                    ('state', 'Result', 120), ('question', 'Question', 400)]:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=60)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        detail = ttk.Frame(panel)
        detail.pack(fill='both', expand=True, pady=(8, 0))
        self.detail = tk.Text(detail, height=9, wrap='word', state='disabled', bg='#152237', fg='#e5eaf3')
        self.detail.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(detail, command=self.detail.yview)
        scroll.pack(side='right', fill='y')
        self.detail.configure(yscrollcommand=scroll.set)
        self.refresh(force=True)

    def rows(self):
        return self.history.rows(None if self.day.get() == 'All dates' else self.day.get())

    def refresh(self, force=False):
        stat = self.history.path.stat()
        signature = (self.day.get(), stat.st_mtime_ns, stat.st_size)
        if not force and signature == self.signature:
            return
        all_rows = self.history.rows()
        dates = sorted({datetime.fromisoformat(row['started']).astimezone(LONDON).date().isoformat() for row in all_rows}, reverse=True)
        self.dates['values'] = ['All dates'] + dates
        rows = self.rows()
        self.signature = signature
        actual = [r for r in rows if r['kind'] != 'historical answer']
        tokens_in = sum(r.get('input_tokens') or 0 for r in actual)
        tokens_out = sum(r.get('output_tokens') or 0 for r in actual)
        unknown = sum(r.get('input_tokens') is None or r.get('output_tokens') is None for r in actual)
        running = sum(r['state'] in ('running', 'awaiting validation') for r in actual)
        self.summary.set(f'{len(actual)} requests · {running} in progress · Reported tokens: {tokens_in:,} input / {tokens_out:,} output'
                         f' · {unknown} requests without complete token counts · {len(rows)-len(actual)} historical answers'
                         + (' · Showing newest 500; CSV includes all selected dates' if len(rows) > 500 else ''))
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.records = {row['id']: row for row in rows[:500]}
        for row in self.records.values():
            stamp = datetime.fromisoformat(row['started']).astimezone(LONDON).strftime('%Y-%m-%d %H:%M:%S')
            self.tree.insert('', 'end', iid=row['id'], values=(stamp, row.get('model', ''), row['kind'], row['state'], row.get('question', '')))
        if selected and selected[0] in self.records:
            self.tree.selection_set(selected[0])
            self.select()
        else:
            self.show_detail('Select a request above.')

    def show_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def select(self, event=None):
        selected = self.tree.selection()
        if not selected or selected[0] not in self.records:
            return
        row = self.records[selected[0]]
        fmt = lambda value: 'Not reported' if value is None else str(value)
        text = (f"Provider: {row.get('provider', '')}\nModel: {row.get('model', '')} ({row.get('model_source', '')})\n"
                f"Started: {row['started']} (UTC)\nDuration: {fmt(row.get('duration_seconds'))} seconds\n"
                f"Request: {row['kind']} | Result: {row['state']}\n"
                f"Tokens: input {fmt(row.get('input_tokens'))}; output {fmt(row.get('output_tokens'))}\n"
                f"Job: {row.get('title', '')} | {row.get('company', '')}\n{row.get('job_url', '')}\n\n"
                f"QUESTION\n{row.get('question', '')}\n\nGENERATED ANSWER\n{row.get('answer') or '(No answer generated)'}\n\n")
        if row.get('effective_answer') and row['effective_answer'] != row.get('answer'):
            text += f"ANSWER RETURNED TO FORM HANDLER\n{row['effective_answer']}\n\n"
        text += f"VALIDATION / ERROR\n{row.get('detail', '')}\n\nMODEL REASON\n{row.get('reason', '')}\n\n"
        text += f"RAW MODEL RESPONSE\n{row.get('raw_response') or '(Not available for historical entries)'}"
        self.show_detail(text)

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='llm-usage.csv', filetypes=[('CSV', '*.csv')])
        if not path:
            return
        fields = ['started', 'provider', 'model', 'kind', 'state', 'title', 'company', 'job_url', 'question',
                  'answer', 'effective_answer', 'detail', 'input_tokens', 'output_tokens', 'duration_seconds', 'raw_response']
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in self.rows():
                    values = {key: row.get(key, '') for key in fields}
                    # Spreadsheet exports must not execute model-generated text.
                    writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
                                     for key, value in values.items()})
        except OSError as exc:
            messagebox.showerror('Could not export LLM usage', str(exc))
