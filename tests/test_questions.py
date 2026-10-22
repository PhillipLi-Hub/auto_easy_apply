import tempfile
import unittest
from pathlib import Path

from easy_apply.models import Settings, normalize
from easy_apply.questions import QUESTION_NOTE_PREFIX, questions_from_note, validate_question_answer
from easy_apply.store import Store


class QuestionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'jobs.sqlite3')
        self.settings = Settings()

    def blocked(self, identifier, labels):
        self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
        self.store.update(str(identifier), 'needs_input', QUESTION_NOTE_PREFIX + ' | '.join(labels))

    def test_legacy_import_deduplicates_retries_and_ranks_distinct_jobs(self):
        question = 'Do you have startup experience?'
        self.blocked(1, ['CV does not establish an answer: ' + question, question, 'Current basic salary*'])
        self.blocked(2, ['DO YOU HAVE STARTUP EXPERIENCE?*'])
        self.blocked(3, ['Unlabelled radio question', 'Enter an OpenAI API key in AI answers, or set OPENAI_API_KEY'])
        self.store.import_question_notes()
        self.store.import_question_notes()
        groups = self.store.unanswered_questions(self.settings)
        self.assertEqual([g['count'] for g in groups], [2, 1])
        self.assertEqual(groups[0]['key'], normalize(question))

    def test_saved_answers_hide_old_questions_and_resolution_survives_restart(self):
        self.blocked(1, ['Current basic salary*'])
        self.store.import_question_notes()
        self.settings.answers['Current basic salary'] = '50000'
        self.assertEqual(self.store.unanswered_questions(self.settings), [])
        self.assertEqual(self.store.resolve_question(normalize('Current basic salary'), self.settings), 1)
        reopened = Store(self.store.path)
        reopened.import_question_notes()
        self.assertEqual(reopened.unanswered_questions(self.settings), [])
        self.assertEqual(reopened.rows()[0]['status'], 'queued')
        self.assertEqual(reopened.rows()[0]['retry_priority'], 1)

    def test_requeue_only_after_all_questions_answered_and_preserve_submissions(self):
        questions = ['Do you need sponsorship?', 'What is your notice period?']
        for identifier in [1, 2, 3, 4]:
            self.blocked(identifier, questions)
        self.store.import_question_notes()
        self.store.update('2', 'applied', source='app_confirmed')
        self.store.update('3', 'unverified')
        self.store.update('4', 'submitting')
        self.settings.answers[questions[0]] = 'Yes'
        self.assertEqual(self.store.resolve_question(normalize(questions[0]), self.settings), 0)
        self.settings.answers[questions[1]] = '3 months'
        self.assertEqual(self.store.resolve_question(normalize(questions[1]), self.settings), 1)
        self.assertEqual({r['id']: r['status'] for r in self.store.rows()},
                         {'1': 'queued', '2': 'applied', '3': 'unverified', '4': 'submitting'})

    def test_form_options_and_failed_saved_answers_can_return_for_correction(self):
        label = 'How many years of Python experience?'
        self.blocked(1, [label])
        spec = {'label': label, 'kind': 'number', 'options': [], 'source': 'form', 'previous_answer': 'five'}
        self.settings.answers[label] = 'five'
        self.store.record_questions('1', [spec, spec])
        self.assertEqual(self.store.unanswered_questions(self.settings)[0]['count'], 1)
        self.settings.answers[label] = '5'
        self.assertEqual(self.store.unanswered_questions(self.settings), [])
        self.store.resolve_question(normalize(label), self.settings)
        self.blocked(1, [label])
        self.store.record_questions('1', [spec | {'previous_answer': '5'}])
        self.assertEqual(len(self.store.unanswered_questions(self.settings)), 1)

    def test_answer_validation_options_numeric_length_and_checkbox(self):
        cases = [('Maybe', {'kind': 'radio', 'options': ['Yes', 'No']}),
                 ('three', {'kind': 'number'}), ('long answer', {'maxlength': 4}),
                 ('sometimes', {'kind': 'checkbox'}), ('   ', {})]
        for answer, extra in cases:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                validate_question_answer(answer, {'label': 'Question'} | extra)
        self.assertEqual(validate_question_answer(' Yes ', {'label': 'Question', 'kind': 'radio', 'options': ['Yes', 'No']}), 'Yes')

    def test_browser_errors_do_not_become_questions(self):
        self.assertEqual(questions_from_note('No visible Easy Apply button after waiting.'), [])
        self.assertEqual(questions_from_note(QUESTION_NOTE_PREFIX + 'Unlabelled radio question'), [])


if __name__ == '__main__':
    unittest.main()
