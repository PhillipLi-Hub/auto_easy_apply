import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from easy_apply.desktop import App
from easy_apply.models import Settings, save_settings, load_settings, normalize
from easy_apply.store import Store
from easy_apply.ai import connection_token


class DesktopTests(unittest.TestCase):
    def test_header_restores_and_updates_confirmed_count_without_replacing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            for identifier, source in [('1', 'app_confirmed'), ('2', 'linkedin_history'), ('3', 'manual_verified')]:
                store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
                store.update(identifier, 'applied', source=source)
            store.add('https://www.linkedin.com/jobs/view/4/')
            store.update('4', 'unverified')
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()):
                app = App(root)
                try:
                    self.assertIn('Today (London): 1  |  All time: 1', app.application_count.get())
                    store.add('https://www.linkedin.com/jobs/view/5/')
                    store.update('5', 'applied', source='app_confirmed')
                    app.status.set('Waiting 10:00 before resuming')
                    app.next_count_refresh = 0
                    for timer in root.tk.call('after', 'info'):
                        root.after_cancel(timer)
                    app.poll()
                    self.assertIn('Today (London): 2  |  All time: 2', app.application_count.get())
                    self.assertEqual(app.status.get(), 'Waiting 10:00 before resuming')
                finally:
                    app.close()
                    root.after(5000, root.quit)
                    root.mainloop()

    def test_todays_completion_is_restored_when_app_reopens(self):
        from easy_apply.limit_report import daily_limit_report, save_limit_report
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            store.add('https://www.linkedin.com/jobs/view/123/')
            save_limit_report(store, daily_limit_report(store, 'You reached today’s Easy Apply limit'))
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()):
                app = App(root)
                try:
                    self.assertTrue(app.status.get().startswith("Today's complete"))
                    self.assertIn('1 jobs remain queued', app.status.get())
                    self.assertFalse(app.busy)
                finally:
                    for timer in root.tk.call('after', 'info'):
                        root.after_cancel(timer)
                    app.close()
                    app.poll()
                    root.after(5000, root.quit)
                    root.mainloop()

    def test_cooldown_countdown_replaces_status_without_flooding_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()):
                app = App(root)
                try:
                    app.busy = True
                    app.status.set('No visible Easy Apply button')
                    activity = app.activity.get('1.0', 'end')
                    for seconds, expected in [(600, '10:00'), (599, '09:59'), (0, 'checking LinkedIn')]:
                        app.messages.put(('cooldown', seconds))
                        for timer in root.tk.call('after', 'info'):
                            root.after_cancel(timer)
                        app.poll()
                        self.assertIn(expected, app.status.get())
                        self.assertTrue(app.busy)
                    self.assertEqual(activity, app.activity.get('1.0', 'end'))
                finally:
                    app.close()
                    root.after(5000, root.quit)
                    root.mainloop()

    def test_llm_usage_shows_full_response_updates_and_exports(self):
        import csv
        from tkinter import ttk
        from easy_apply.usage_ui import UsagePanel
        from tests.test_ai import completed
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                panel = UsagePanel(ttk.Frame(root), Path(directory))
                identifier = panel.history.begin('ollama', 'qwen', {'title': 'Quant'}, 'Why this role?', 'generation')
                panel.refresh()
                self.assertIn('1 requests', panel.summary.get())
                panel.tree.selection_set(identifier)
                panel.select()
                self.assertIn('running', panel.detail.get('1.0', 'end'))
                panel.history.response(identifier, completed('=Unsafe spreadsheet formula') | {'usage': {'input_tokens': 10, 'output_tokens': 4}}, 1.5)
                panel.history.update(identifier, 'rejected', detail='Unsupported answer')
                panel.refresh()
                detail = panel.detail.get('1.0', 'end')
                self.assertIn('Why this role?', detail)
                self.assertIn('=Unsafe spreadsheet formula', detail)
                self.assertIn('Unsupported answer', detail)
                self.assertIn('Model: qwen', detail)
                path = Path(directory) / 'usage.csv'
                with patch('easy_apply.usage_ui.filedialog.asksaveasfilename', return_value=str(path)):
                    panel.export()
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual(exported[0]['answer'], "'=Unsafe spreadsheet formula")
                self.assertEqual(exported[0]['input_tokens'], '10')
            finally:
                root.destroy()

    def test_daily_reports_tab_shows_counts_and_keeps_daily_limit_status(self):
        from easy_apply.daily_report import local_day
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            for identifier, title in [('123', 'Quant Analyst'), ('456', 'Data Scientist')]:
                store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
                store.update(identifier, 'queued', job={'title': title, 'company': 'Example'})
                store.claim(identifier)
                store.update(identifier, 'applied', source='app_confirmed')
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()):
                app = App(root)
                try:
                    self.assertIn('Daily reports', [app.tabs.tab(tab, 'text') for tab in app.tabs.tabs()])
                    self.assertIn('Quant: 1', app.report_text.get('1.0', 'end'))
                    self.assertIn('Data Scientist: 1', app.report_text.get('1.0', 'end'))
                    app.run_result = 'LinkedIn daily limit reached'
                    app.messages.put(('daily_report', {'date': local_day()}))
                    app.messages.put(('idle', 'Ready'))
                    for timer in root.tk.call('after', 'info'):
                        root.after_cancel(timer)
                    app.poll()
                    self.assertEqual(app.tabs.select(), str(app.reports_panel))
                    self.assertEqual(app.status.get(), 'LinkedIn daily limit reached')
                    app.refresh_daily_report(generate=True)
                    self.assertTrue((store.path.parent / 'reports' / 'daily' / f'{local_day()}.html').exists())
                finally:
                    app.close()
                    root.after(5000, root.quit)
                    root.mainloop()

    def test_schedule_uses_saved_settings_and_starts_only_one_worker_command(self):
        import queue
        import threading
        from unittest.mock import Mock
        saved = Settings(first_name='Ada', last_name='Example', email='ada@example.com', keywords='quant', location='London')
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            root = tk.Tk()
            root.withdraw()
            worker = Mock()
            worker.commands = queue.Queue()
            worker.stop_event = threading.Event()
            worker.is_alive.return_value = False
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=saved), \
                 patch('easy_apply.desktop.BrowserWorker', return_value=worker):
                app = App(root)
                try:
                    app.variables['keywords'].set('unsaved edit')
                    day, _ = app.schedule.request()
                    app.check_schedule()
                    action, settings, request_day = worker.commands.get_nowait()
                    self.assertEqual(action, 'scheduled_apply')
                    self.assertEqual(settings.keywords, 'quant')
                    self.assertEqual(request_day, day)
                    self.assertTrue(app.busy)
                    app.next_schedule_check = 0
                    app.check_schedule()
                    self.assertTrue(worker.commands.empty())
                finally:
                    for timer in root.tk.call('after', 'info'):
                        root.after_cancel(timer)
                    root.destroy()

    def test_daily_limit_notification_and_status_survive_idle(self):
        from easy_apply.limit_report import daily_limit_report
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            store.add('https://www.linkedin.com/jobs/view/123/')
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()), \
                 patch('easy_apply.desktop.App.show_daily_limit_notice') as notice:
                app = App(root)
                try:
                    app.busy = True
                    report = daily_limit_report(store, 'You reached today’s Easy Apply limit', 3)
                    app.messages.put(('daily_limit', report))
                    app.messages.put(('idle', 'Ready'))
                    for timer in root.tk.call('after', 'info'):
                        root.after_cancel(timer)
                    app.poll()
                    notice.assert_called_once()
                    self.assertIn('LinkedIn daily limit reached', notice.call_args.args[0])
                    self.assertIn('Confirmed applications this run: 3', notice.call_args.args[0])
                    self.assertIn('daily limit reached', app.status.get())
                    self.assertIn('1 jobs remain queued', app.status.get())
                    self.assertFalse(app.busy)
                    self.assertIn('today’s Easy Apply limit', app.activity.get('1.0', 'end'))
                finally:
                    app.close()
                    root.after(5000, root.quit)
                    root.mainloop()

    def test_question_tab_saves_reuses_answer_removes_row_and_requeues(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            settings_path = Path(directory) / 'settings.json'
            question = 'Have you worked in a startup?'
            for identifier in ['123', '456']:
                store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
                store.update(identifier, 'needs_input', 'Save exact answers or resolve these fields: ' + question)
                store.record_questions(identifier, [{'label': question, 'kind': 'radio', 'options': ['Yes', 'No'], 'source': 'form'}])
            root = tk.Tk()
            root.withdraw()
            with patch('easy_apply.desktop.Store', return_value=store), patch('easy_apply.desktop.load_settings', return_value=Settings()), \
                 patch('easy_apply.desktop.save_settings', side_effect=lambda settings: save_settings(settings, settings_path)), \
                 patch('easy_apply.desktop.messagebox.showerror') as error:
                app = App(root)
                try:
                    self.assertIn('Needs answers', [app.tabs.tab(tab, 'text') for tab in app.tabs.tabs()])
                    key = normalize(question)
                    self.assertEqual(app.question_groups[key]['count'], 2)
                    app.question_tree.selection_set(key)
                    app.select_question()
                    app.question_answer.insert('1.0', 'Maybe')
                    app.save_question_answer()
                    error.assert_called_once()
                    self.assertTrue(app.question_tree.exists(key))
                    self.assertFalse(settings_path.exists())
                    app.question_answer.delete('1.0', 'end')
                    app.question_answer.insert('1.0', 'Yes')
                    app.save_question_answer()
                    self.assertFalse(app.question_tree.exists(key))
                    self.assertEqual(load_settings(settings_path).answer(question + '*'), 'Yes')
                    self.assertEqual(app.collect().answer(question), 'Yes')
                    self.assertEqual(len(store.rows('queued')), 2)
                    app.refresh()
                    self.assertEqual(app.question_tree.get_children(), ())
                finally:
                    app.close()
                    root.after(5000, root.quit)
                    root.mainloop()

    @patch.dict('os.environ', {}, clear=True)
    def test_startup_profile_fields_queue_and_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            root = tk.Tk()
            root.withdraw()
            with patch("easy_apply.desktop.Store", return_value=store), patch("easy_apply.desktop.load_settings", return_value=Settings()):
                app = App(root)
                root.update_idletasks()
                self.assertEqual(app.collect(), Settings())
                app.url.set("https://www.linkedin.com/jobs/view/123/")
                app.add_job()
                self.assertTrue(app.tree.exists("123"))
                app.variables["first_name"].set("Ada")
                self.assertEqual(app.collect().first_name, "Ada")
                self.assertIn('Local Ollama', app.ai_status.get())
                self.assertNotIn('enter an API key', app.ai_status.get())
                app.ai_provider.set('openai')
                app.ai_enabled.set(True)
                self.assertIn('not connected', app.ai_status.get())
                app.api_key.set('fake-test-key')
                self.assertIn('connection not tested', app.ai_status.get())
                token = connection_token(app.collect())
                app.messages.put(('ai_connection', (token, 'verified', 'test model')))
                app.poll()
                self.assertIn('connection verified', app.ai_status.get())
                app.variables['ai_model'].set('other-model')
                self.assertIn('connection not tested', app.ai_status.get())
                # A delayed reply from the previous configuration cannot verify the new one.
                app.messages.put(('ai_connection', (token, 'verified', 'old model')))
                app.poll()
                self.assertIn('connection not tested', app.ai_status.get())
                app.api_key.set('')
                self.assertIn('not connected', app.ai_status.get())
                app.close()
                root.after(5000, root.quit)
                root.mainloop()
                self.assertFalse(app.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
