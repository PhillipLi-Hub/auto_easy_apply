import unittest

from easy_apply.answer_format import format_answer


class AnswerFormatTests(unittest.TestCase):
    def test_salary_compacts_without_changing_amount_or_minimum(self):
        info = {'label': 'What are your salary expectations?', 'type': 'text', 'maxlength': 20}
        self.assertEqual(format_answer('At least GBP 75,000 per year.', info), '75000')
        self.assertEqual(format_answer('At least GBP 75,000 per year.', info | {'type': 'number'}), '75000')
        self.assertEqual(format_answer('GBP 50,000 per year', info | {'maxlength': 16}), '50000')

    def test_long_answers_are_not_truncated_and_currencies_not_converted(self):
        info = {'label': 'Salary', 'type': 'text', 'maxlength': 20}
        for answer, change in [
            ('At least GBP 75,000 per year.', {'label': 'Salary in euros'}),
            ('My project involved a complex migration.', {'label': 'Describe your project'}),
            ('At least GBP 75,000 per year.', {'maxlength': 4}),
            ('GBP 75,000 to GBP 95,000 per year', {}),
        ]:
            with self.subTest(answer=answer, change=change), self.assertRaises(ValueError):
                format_answer(answer, info | change)
