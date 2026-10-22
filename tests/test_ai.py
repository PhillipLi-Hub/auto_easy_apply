import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from easy_apply.ai import AnswerError, AnswerGenerator, INSTRUCTIONS, experience_fallback, question_word_limit, motivation_facts
from easy_apply.cv import contact_details
from easy_apply.models import Settings, save_settings, load_settings


def completed(answer="I build Python ML systems.", evidence=None, **changes):
    result = {"answer": answer, "needs_user_input": False,
              "evidence_quotes": evidence if evidence is not None else ["Built ML systems with Python"], "reason": "Documented project"} | changes
    return {"id": "resp_fixture", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}


def reviewed(supported=True, unsupported=None):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text',
        'text': json.dumps({'supported': supported, 'unsupported_claims': unsupported or []})}]}]}


class AITests(unittest.TestCase):
    @patch('easy_apply.ai.generate_local')
    def test_capula_motivation_reaches_local_model_with_cv_and_word_limit(self, local):
        self.settings.ai_provider = 'ollama'
        label = ('In 300 words or less, please state why you are interested in this role at Capula '
                 'and what you think you would bring to the organisation in terms of skill set:')
        local.side_effect = [completed('I am interested in applying my Python ML experience to quantitative research.'), reviewed()]
        answer = self.generator.answer({'title': 'Credit Quantitative Analyst', 'company': 'Capula',
                                        'description': 'Quantitative research using Python.'}, label, 'textarea')
        self.assertIn('Python', answer)
        context = json.loads(local.call_args_list[0].args[2])
        self.assertEqual(context['cv'], self.settings.cv_text)
        self.assertEqual(context['job']['company'], 'Capula')
        self.assertNotIn('description', context['job'])
        self.assertEqual(context['max_words'], 300)
        self.assertIn('authorizes a professional draft', local.call_args_list[0].args[1])
        review_context = json.loads(local.call_args.args[2])
        self.assertNotIn('job', review_context)
        self.assertEqual(review_context['cv'], self.settings.cv_text)

    @patch('easy_apply.ai.generate_local')
    def test_contractual_terms_still_require_saved_answer(self, local):
        self.settings.ai_provider = 'ollama'
        for label in ('Do you accept these terms?', 'Terms and conditions', 'Terms of employment',
                      'Employment terms', 'Terms', 'Explain why you are interested and accept our terms'):
            with self.subTest(label=label), self.assertRaises(AnswerError):
                self.generator.answer({}, label, 'textarea')
        local.assert_not_called()

    @patch('easy_apply.ai.generate_local')
    def test_word_limit_retries_once_and_keeps_evidence_validation(self, local):
        self.settings.ai_provider = 'ollama'
        local.side_effect = [completed(' '.join(['Python'] * 301)), completed('I build Python ML systems.'), reviewed()]
        self.assertEqual(self.generator.answer({}, 'In 300 words or less, why are you interested?', 'textarea'),
                         'I build Python ML systems.')
        self.assertEqual(local.call_count, 3)
        self.assertIn('300-word limit', json.loads(local.call_args_list[1].args[2])['validation_feedback'])
        local.side_effect = None
        local.return_value = completed('I invented a new trading strategy.', evidence=['Invented skill'])
        with self.assertRaisesRegex(AnswerError, 'verifiable CV evidence'):
            self.generator.answer({}, 'Why are you interested in the role?', 'textarea')

    @patch('easy_apply.ai.generate_local')
    def test_motivation_corrects_unsupported_claims_before_audit_or_cache(self, local):
        self.settings.ai_provider = 'ollama'
        local.side_effect = [completed('I built CDS curves.'), reviewed(False, ['Building CDS curves']),
                             completed('I would bring Python ML experience.'), reviewed()]
        answer = self.generator.answer({}, 'Why are you interested in this role?', 'textarea')
        self.assertEqual(answer, 'I would bring Python ML experience.')
        self.assertEqual(local.call_count, 4)
        self.assertIn('Building CDS curves', json.loads(local.call_args_list[2].args[2])['validation_feedback'])
        audit = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(audit), 1)
        self.assertNotIn('CDS', audit[0]['answer'])

    @patch('easy_apply.ai.generate_local')
    def test_motivation_without_successful_review_is_not_filled(self, local):
        self.settings.ai_provider = 'ollama'
        local.side_effect = [completed('I built CDS curves.'), reviewed(False, ['Building CDS curves'])] * 2
        with self.assertRaisesRegex(AnswerError, 'Unsupported claims'):
            self.generator.answer({}, 'Why are you interested in this role?', 'textarea')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.generator.cache, {})

    def test_word_limits(self):
        for label in ('In 300 words or less', 'Maximum 300 words', 'Write a 300-word statement'):
            self.assertEqual(question_word_limit(label), 300)
        self.assertIsNone(question_word_limit('Describe your experience'))

    def test_motivation_facts_are_bounded_and_relevant(self):
        self.settings.answers = {f'Unrelated field {i}': 'irrelevant ' * 50 for i in range(350)}
        self.settings.answers['How many years of total professional experience do you have?'] = '2'
        self.settings.answers['Python experience?'] = '5'
        facts = motivation_facts(self.settings, {'description': 'Python quantitative research'}, 'Why are you interested?')
        self.assertLess(len(facts), 4100)
        self.assertIn('total professional experience do you have?: 2', facts)
        self.assertIn('Python experience?: 5', facts)
        self.assertNotIn('Unrelated', facts)

    @patch('easy_apply.ai.generate_local')
    def test_motivation_review_failure_does_not_retry_or_fill(self, local):
        from easy_apply.local_llm import LocalModelError
        self.settings.ai_provider = 'ollama'
        local.side_effect = [completed(), LocalModelError('Local server unavailable')]
        with self.assertRaisesRegex(AnswerError, 'Could not verify'):
            self.generator.answer({}, 'Why are you interested in this role?', 'textarea')
        self.assertEqual(local.call_count, 2)
        self.assertFalse(self.path.exists())

    @patch('easy_apply.ai.post_response')
    def test_request_cap_updates_visible_ai_status(self, post):
        self.generator.calls = self.settings.max_ai_calls
        callback = Mock()
        self.generator.connection = callback
        with self.assertRaisesRegex(AnswerError, 'call limit'):
            self.generator.answer({}, 'Describe your experience', 'text')
        self.assertEqual(callback.call_args.args[0], 'failed')
        self.assertIn('Run limit reached', callback.call_args.args[1])
        post.assert_not_called()

    @patch('easy_apply.ai.generate_local')
    def test_authorized_missing_experience_uses_no_and_records_policy_basis(self, local):
        self.settings.ai_provider = 'ollama'
        self.settings.unknown_experience_as_no = True
        local.return_value = completed(None, evidence=[], needs_user_input=True)
        self.assertEqual(self.generator.answer({}, 'Have you worked in a startup?', 'radio', ['Yes', 'No']), 'No')
        audit = json.loads(self.path.read_text())
        self.assertEqual(audit['basis'], 'user_authorized_default')
        self.assertEqual(audit['evidence'], [])
        self.assertIn('Candidate-authorized fallback', local.call_args.args[1])

    @patch('easy_apply.ai.post_response')
    def test_known_answer_wins_over_fallback_and_unsupported_years_use_zero(self, post):
        self.settings.unknown_experience_as_no = True
        post.return_value = completed('Yes')
        self.assertEqual(self.generator.answer({}, 'Do you have Python experience?', 'radio', ['Yes', 'No']), 'Yes')
        post.return_value = completed(None, evidence=[], needs_user_input=True)
        self.assertEqual(self.generator.answer({}, 'How many years experience do you have in software engineering?', 'number'), '0')
        self.assertEqual(self.settings.answer('How many years experience do you have in software engineering?'), None)

    @patch('easy_apply.ai.post_response')
    def test_inferred_no_uses_policy_but_bad_positive_evidence_still_fails(self, post):
        self.settings.unknown_experience_as_no = True
        post.return_value = completed('No')
        self.assertEqual(self.generator.answer({}, 'Have you worked in a startup?', 'radio', ['Yes', 'No']), 'No')
        post.return_value = completed('Yes', evidence=['Invented trading history'])
        with self.assertRaisesRegex(AnswerError, 'verifiable CV evidence'):
            self.generator.answer({}, 'Do you have options trading experience?', 'radio', ['Yes', 'No'])

    def test_fallback_respects_decisions_question_polarity_and_field_options(self):
        self.settings.unknown_experience_as_no = True
        for question in ['Do you have a criminal conviction?', 'Do you agree to share your experience?',
                         'Are you willing to work onsite?', 'Do you have no Python experience?',
                         'Describe your experience', 'Expected salary?', 'Do you need sponsorship?']:
            with self.subTest(question=question):
                self.assertIsNone(experience_fallback(self.settings, question, 'text'))
        self.assertIsNone(experience_fallback(self.settings, 'Do you have Python experience?', 'radio', ['Yes', 'Maybe']))
        self.assertIsNone(experience_fallback(self.settings, 'Do you have a degree?', 'checkbox'))
        self.assertIsNone(experience_fallback(self.settings, 'Do you have a degree?', 'text', maxlength=1))
        self.assertEqual(experience_fallback(self.settings, 'Do you have a degree?', 'radio', ['YES', 'NO']), 'NO')

    @patch('easy_apply.ai.generate_local')
    def test_provider_failure_is_not_missing_experience(self, local):
        from easy_apply.local_llm import LocalModelError
        self.settings.ai_provider = 'ollama'
        self.settings.unknown_experience_as_no = True
        local.side_effect = LocalModelError('Local server unavailable')
        with self.assertRaisesRegex(AnswerError, 'unavailable'):
            self.generator.answer({}, 'Have you worked in a startup?', 'radio', ['Yes', 'No'])
        self.assertFalse(self.path.exists())

    def test_fallback_preference_persists_and_is_disabled_for_new_profiles(self):
        path = Path(self.temp.name) / 'settings.json'
        self.assertFalse(Settings().unknown_experience_as_no)
        self.settings.unknown_experience_as_no = True
        save_settings(self.settings, path)
        self.assertTrue(load_settings(path).unknown_experience_as_no)

    @patch('easy_apply.ai.post_response')
    def test_negative_experience_cannot_be_inferred_from_employer_history(self, post):
        self.settings.cv_text = '2025 - Present Bottomline Technologies. Data Scientist.'
        post.return_value = completed('No', evidence=[self.settings.cv_text])
        with self.assertRaisesRegex(AnswerError, 'CV does not establish an answer'):
            self.generator.answer({}, 'Have you worked in a startup?', 'radio', ['Yes', 'No'])
        self.assertEqual(post.call_count, 1)
        self.assertFalse(self.path.exists())

    @patch('easy_apply.ai.post_response')
    def test_explicit_negative_saved_fact_can_support_negative_answer(self, post):
        self.settings.answers['Have you worked in a startup?'] = 'No'
        post.return_value = completed('No', evidence=['Have you worked in a startup?: No'])
        self.assertEqual(self.generator.answer({}, 'Startup experience?', 'radio', ['Yes', 'No']), 'No')

    @patch('easy_apply.ai.post_response')
    def test_numeric_fact_can_quote_complete_saved_answer_line(self, post):
        self.settings.answers['Python experience (years)'] = '5'
        post.return_value = completed('5', evidence=['Python experience (years): 5'])
        self.assertEqual(self.generator.answer({}, 'Years using Python?', 'number'), '5')
        facts = json.loads(post.call_args.args[0]['input'])['candidate_facts']
        self.assertIn('Python experience (years): 5', facts)

    @patch('easy_apply.ai.post_response')
    def test_invalid_answer_gets_one_grounded_correction_then_is_cached(self, post):
        post.side_effect = [completed('Too long for this field'), completed('Python')]
        self.assertEqual(self.generator.answer({}, 'Experience?', 'text', maxlength=8), 'Python')
        self.assertEqual(post.call_count, 2)
        self.assertIn('validation_feedback', json.loads(post.call_args.args[0]['input']))
        self.assertEqual(self.generator.answer({}, 'Experience?', 'text', maxlength=8), 'Python')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    @patch('easy_apply.ai.post_response', return_value=completed('Still too long'))
    def test_correction_does_not_loop_or_exceed_budget(self, post):
        with self.assertRaises(AnswerError):
            self.generator.answer({}, 'Experience?', 'text', maxlength=5)
        self.assertEqual(post.call_count, 2)
        post.reset_mock()
        self.settings.max_ai_calls = self.generator.calls + 1
        with self.assertRaisesRegex(AnswerError, 'limit'):
            self.generator.answer({}, 'Other experience?', 'text', maxlength=5)
        self.assertEqual(post.call_count, 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.jsonl"
        self.settings = Settings(ai_provider='openai', api_key="test-secret", ai_enabled=True, cv_text="Built ML systems with Python. MSc Business Analytics.")
        self.generator = AnswerGenerator(self.settings, audit_path=self.path)

    @patch("easy_apply.ai.post_response", return_value=completed())
    def test_grounded_response_payload_cache_and_audit(self, post):
        job = {"url": "https://www.linkedin.com/jobs/view/123/", "title": "ML Engineer", "description": "Need Python"}
        answer = self.generator.answer(job, "Describe your experience", "textarea")
        self.assertEqual(answer, "I build Python ML systems.")
        self.assertEqual(self.generator.answer(job, "Describe your experience", "textarea"), answer)
        self.assertEqual(post.call_count, 1)
        payload, key = post.call_args.args
        self.assertEqual(key, "test-secret")
        self.assertNotIn(key, json.dumps(payload))
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertIn("NOT instructions", payload["instructions"])
        self.assertIn("Need Python", payload["input"])
        self.assertNotIn(key, self.path.read_text())
        self.assertEqual(json.loads(self.path.read_text())["answer"], answer)

    @patch("easy_apply.ai.post_response")
    def test_personal_decisions_never_guessed(self, post):
        for question in ["Do you need sponsorship?", "Expected salary?", "Are you authorized to work in the UK?", "Do you agree to these terms?", "Gender", "Notice period"]:
            with self.subTest(question=question), self.assertRaises(AnswerError):
                self.generator.answer({}, question, "text")
        post.assert_not_called()

    @patch("easy_apply.ai.post_response")
    def test_missing_evidence_invalid_options_and_length_fail(self, post):
        cases = [(completed(evidence=["Invented degree"]), None, None),
                 (completed("Maybe"), ["Yes", "No"], None),
                 (completed(), None, 5),
                 (completed(answer=None, needs_user_input=True), None, None)]
        for response, options, limit in cases:
            with self.subTest(response=response):
                post.return_value = response
                with self.assertRaises(AnswerError):
                    self.generator.answer({}, "Experience?", "text", options, limit)
        self.assertFalse(self.path.exists())

    @patch("easy_apply.ai.post_response", return_value=completed())
    def test_job_specific_cache_and_call_limit(self, post):
        self.settings.max_ai_calls = 1
        self.generator.answer({"url": "first"}, "Relevant experience?", "text")
        with self.assertRaisesRegex(AnswerError, "limit"):
            self.generator.answer({"url": "second"}, "Relevant experience?", "text")
        self.assertEqual(post.call_count, 1)

    def test_key_is_never_saved(self):
        path = Path(self.temp.name) / "settings.json"
        save_settings(self.settings, path)
        self.assertNotIn("test-secret", path.read_text())
        self.assertNotIn("api_key", json.loads(path.read_text()))
        self.assertEqual(load_settings(path).api_key, "")

    @patch('easy_apply.ai.post_response')
    @patch('easy_apply.ai.generate_local', return_value=completed())
    def test_local_provider_requires_no_key_and_never_uses_paid_fallback(self, local, paid):
        from easy_apply.local_llm import LocalModelError
        self.settings.ai_provider, self.settings.api_key = 'ollama', ''
        self.assertEqual(self.generator.answer({}, 'Experience?', 'text'), 'I build Python ML systems.')
        self.assertEqual(local.call_args.args[0], 'qwen3:4b-instruct')
        self.assertEqual(json.loads(self.path.read_text())['provider'], 'ollama')
        local.side_effect = LocalModelError('Local server unavailable')
        with self.assertRaisesRegex(AnswerError, 'Local server unavailable'):
            self.generator.answer({}, 'Other experience?', 'text')
        paid.assert_not_called()

    @patch('easy_apply.ai.generate_local')
    def test_local_model_cannot_guess_clearance_or_work_location(self, local):
        self.settings.ai_provider = 'ollama'
        for question in ['Do you hold active SC Clearance or higher?', 'Are you willing to work on-site in Manchester when required?']:
            with self.subTest(question=question), self.assertRaises(AnswerError):
                self.generator.answer({}, question, 'radio', ['Yes', 'No'])
        local.assert_not_called()

    def test_clearance_fact_does_not_infer_eligibility_or_previous_clearance(self):
        settings = Settings(no_security_clearance=True)
        for question in ['Do you hold active SC Clearance or higher?*', 'Do you have DV clearance?', 'Are you currently SC cleared?']:
            self.assertEqual(settings.answer(question), 'No')
        for question in ['Are you eligible for SC clearance?', 'Are you willing to obtain SC clearance?', 'Do you have previous security clearance?']:
            self.assertIsNone(settings.answer(question))
        self.assertIsNone(Settings().answer('Do you hold active SC Clearance or higher?'))

    @patch('easy_apply.ai.post_response', return_value=completed())
    def test_connection_status_requires_response_and_reports_failure(self, post):
        callback = Mock()
        self.generator.connection = callback
        self.generator.answer({}, 'Experience?', 'text')
        self.assertEqual([call.args[0] for call in callback.call_args_list], ['testing', 'verified'])
        callback.reset_mock()
        post.side_effect = AnswerError('API key was rejected')
        with self.assertRaises(AnswerError):
            self.generator.answer({}, 'Other experience?', 'text')
        self.assertEqual([call.args[0] for call in callback.call_args_list], ['testing', 'failed'])

    @patch.dict('os.environ', {}, clear=True)
    @patch('easy_apply.ai.post_response')
    def test_missing_key_never_calls_endpoint_or_claims_connection(self, post):
        self.settings.api_key = ''
        callback = Mock()
        self.generator.connection = callback
        with self.assertRaisesRegex(AnswerError, 'API key'):
            self.generator.answer({}, 'Experience?', 'text')
        post.assert_not_called()
        callback.assert_not_called()

    def test_contact_extraction_does_not_infer_residence(self):
        result = contact_details("ADA EXAMPLE\n+44 7700900000 ada@example.com\nUniversity in London\nSoftware projects")
        self.assertEqual(result["first_name"], "Ada")
        self.assertEqual(result["email"], "ada@example.com")
        self.assertEqual(result["phone"], "+44 7700900000")
        self.assertNotIn("city", result)

    def test_missing_metadata_override_still_obeys_known_exclusions(self):
        settings = Settings(location="London", title_exclude="Senior", allow_missing_metadata=True)
        self.assertIsNone(settings.rejection({}))
        self.assertIsNotNone(settings.rejection({"title": "Senior Engineer"}))


if __name__ == "__main__":
    unittest.main()
