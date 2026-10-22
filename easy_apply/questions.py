"""Question inbox helpers shared by the database, form reader and desktop UI."""

import re

from .answer_format import format_answer
from .models import normalize


QUESTION_NOTE_PREFIX = 'Save exact answers or resolve these fields: '
ERROR_PREFIXES = (
    'CV does not establish an answer: ',
    'Save your own exact answer for: ',
    'Generated answer lacked verifiable CV evidence: ',
)
FIELD_ERRORS = (
    ': saved answer does not match exactly one option',
    ': could not verify the selected radio answer',
    ': checkbox answer must be yes or no',
    ': this field requires a numeric answer',
    ': answer exceeds ',
    ': the form changed or truncated the answer',
    ': could not identify one matching city and country suggestion',
    ': choose an autocomplete suggestion manually',
)


def questions_from_note(note):
    """Import older notes conservatively; browser errors are not questions."""
    if not note.startswith(QUESTION_NOTE_PREFIX):
        return []
    found = {}
    for part in note[len(QUESTION_NOTE_PREFIX):].split(' | '):
        label = part.strip()
        for prefix in ERROR_PREFIXES:
            if label.startswith(prefix):
                label = label[len(prefix):].strip()
                break
        for suffix in FIELD_ERRORS:
            if suffix in label:
                label = label.split(suffix, 1)[0].strip()
                break
        if not label or label.startswith(('Unlabelled', 'Unsupported', 'Choose your', 'Enter an OpenAI',
                                         'Import your CV', 'AI ', 'Local ', 'OpenAI ', 'Generated answer',
                                         'Could not ', 'Save your own exact answer for: unlabelled')):
            continue
        if normalize(label) in {'resume', 'résumé', 'cv', 'curriculum', 'curriculum vitae', 'lebenslauf'}:
            continue
        # Notes without an identifiable question/field cannot be answered here.
        if not (re.search(r'[?*]', label) or re.match(r'(?i)^(?:do |have |are |will |would |can |how |what |when |where |which |please |current |expected |desired |notice |email |phone |salary|nationality|gender)', label)):
            continue
        found.setdefault(normalize(label), {'label': label, 'kind': 'text', 'options': [],
                                           'maxlength': None, 'source': 'legacy', 'issue': part})
    return list(found.values())


def validate_question_answer(answer, spec):
    answer = answer.strip()
    if not answer:
        raise ValueError('Enter an answer before saving.')
    options = spec.get('options') or []
    if options and sum(normalize(option) == normalize(answer) for option in options) != 1:
        raise ValueError('Use one of the displayed options exactly.')
    if spec.get('kind') == 'checkbox' and normalize(answer) not in {'yes', 'no', 'true', 'false'}:
        raise ValueError('Enter Yes or No for this checkbox.')
    format_answer(answer, {'type': spec.get('kind', 'text'), 'label': spec['label'],
                           'maxlength': spec.get('maxlength')})
    return answer


def answered(spec, settings):
    value = settings.answer(spec['label'])
    if value is None:
        return False
    # If LinkedIn rejected this exact saved value, keep it visible for correction.
    if spec.get('source') != 'legacy' and value == spec.get('previous_answer'):
        return False
    try:
        validate_question_answer(value, spec)
        return True
    except ValueError:
        return False
