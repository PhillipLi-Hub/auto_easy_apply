import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from easy_apply.ai import AnswerGenerator, AnswerError
from easy_apply.llm_usage import UsageHistory
from easy_apply.local_llm import LocalModelError
from easy_apply.models import Settings
from tests.test_ai import completed, reviewed


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.settings = Settings(ai_provider='ollama', cv_text='Built ML systems with Python.')
        self.generator = AnswerGenerator(self.settings, audit_path=self.directory / 'ai_answers.jsonl')

    @patch('easy_apply.ai.generate_local')
    def test_pending_then_response_model_tokens_cache_and_no_secrets(self, generate):
        def respond(*args):
            pending = self.generator.usage.rows()[0]
            self.assertEqual(pending['state'], 'running')
            self.assertEqual(pending['question'], 'Describe Python experience')
            self.assertNotIn('cv', pending)
            return completed() | {'model': 'actual-model', 'usage': {'input_tokens': 100, 'output_tokens': 20}}
        generate.side_effect = respond
        job = {'title': 'Quant', 'company': 'Example', 'url': 'https://www.linkedin.com/jobs/view/123/'}
        for _ in range(2):
            self.generator.answer(job, 'Describe Python experience', 'textarea')
        rows = self.generator.usage.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['state'], 'accepted')
        self.assertEqual(row['model'], 'actual-model')
        self.assertEqual(row['input_tokens'], 100)
        self.assertEqual(row['effective_answer'], 'I build Python ML systems.')
        self.assertGreaterEqual(row['duration_seconds'], 0)
        self.assertEqual(row['company'], 'Example')

    @patch('easy_apply.ai.generate_local')
    def test_rejected_generation_correction_and_review_are_separate(self, generate):
        generate.side_effect = [completed('Too long for this field'), completed('Python')]
        self.generator.answer({}, 'Describe skills', 'text', maxlength=8)
        rows = self.generator.usage.rows()
        self.assertEqual({r['kind'] for r in rows}, {'generation', 'correction'})
        rejected = next(r for r in rows if r['state'] == 'rejected')
        self.assertEqual(rejected['answer'], 'Too long for this field')
        self.assertIn('length limit', rejected['detail'])
        generate.side_effect = [completed(), reviewed()]
        self.generator.answer({}, 'Why are you interested?', 'textarea')
        rows = self.generator.usage.rows()
        review = next(r for r in rows if r['kind'] == 'evidence review')
        self.assertTrue(review['review']['supported'])
        parent = next(r for r in rows if r['id'] == review['parent_id'])
        self.assertEqual(parent['question'], review['question'])

    @patch('easy_apply.ai.generate_local')
    def test_failed_request_and_fallback_are_not_reported_as_generated_answers(self, generate):
        generate.side_effect = LocalModelError('Local server unavailable')
        with self.assertRaises(AnswerError):
            self.generator.answer({}, 'Describe skills', 'textarea')
        self.assertEqual(self.generator.usage.rows()[0]['state'], 'failed')
        self.assertIsNone(self.generator.usage.rows()[0]['input_tokens'])
        self.settings.unknown_experience_as_no = True
        generate.side_effect = None
        generate.return_value = completed(None, evidence=[], needs_user_input=True)
        self.generator.answer({}, 'Have you worked in a startup?', 'radio', ['Yes', 'No'])
        row = self.generator.usage.rows()[0]
        self.assertEqual(row['state'], 'fallback')
        self.assertIsNone(row['answer'])
        self.assertEqual(row['effective_answer'], 'No')

    def test_legacy_import_is_once_only_and_dates_use_london(self):
        path = self.directory / 'legacy.jsonl'
        entry = dict(at='2026-10-18T23:30:00+00:00', question='Question?', answer='Answer',
                     provider='ollama', model='qwen', usage={'input_tokens': 5})
        path.write_text(json.dumps(entry) + '\nnot json\n', encoding='utf-8')
        history = self.generator.usage
        history.import_legacy(path)
        history.import_legacy(path)
        self.assertEqual(len(history.rows('2026-10-19')), 1)
        self.assertEqual(len(history.rows('2026-10-18')), 0)
        self.assertEqual(history.rows()[0]['kind'], 'historical answer')
        request = history.begin('ollama', 'qwen', {}, 'A new request', 'generation')
        history.recover()
        self.assertEqual(next(r for r in history.rows() if r['id'] == request)['state'], 'interrupted')

    @patch('easy_apply.ai.post_response')
    def test_openai_usage_does_not_store_api_key(self, post):
        self.settings.ai_provider = 'openai'
        self.settings.api_key = 'secret-never-log-this'
        post.return_value = completed() | {'usage': {'input_tokens': 55, 'output_tokens': 9}}
        self.generator.answer({}, 'Describe skills', 'text')
        self.assertNotIn(self.settings.api_key, json.dumps(self.generator.usage.rows()))
        self.assertEqual(self.generator.usage.rows()[0]['provider'], 'openai')
