"""Identify explicit SC requirements, rather than screening questions."""

import re


SC = r'\b(?:sc(?:\s*[/&]\s*dv)?(?:[ -]+(?:security[ -]+)?clearance|[ -]+cleared)?|security check(?:\s*\(sc\))?(?: clearance)?)\b'
REASON = 'Excluded: job requires SC clearance'


def requires_sc(title='', description=''):
    for is_title, text in ((True, title), (False, description)):
        for clause in re.split(r'[\n.!;]+', text.casefold()):
            clause = ' '.join(clause.split())
            if not re.search(SC, clause) or '?' in clause:
                continue
            # Optional clearance and eligibility to obtain it do not establish
            # a requirement to hold clearance. Check each statement separately.
            if re.search(r'\b(?:not required|not essential|not necessary|no .*required|without|desirable|preferred|preferably|advantageous|nice to have|bonus|eligible|eligibility|willing|ability to obtain|able to obtain)\b', clause):
                continue
            if is_title and re.search(r'\bsc[ -]+(?:cleared|clearance)\b', clause):
                return True
            if re.search(rf'{SC}.{{0,65}}\b(?:required|essential|mandatory|necessary|must|needed|only)\b', clause):
                return True
            if re.search(rf'\b(?:must|require[ds]?|essential|mandatory|need to|only consider).{{0,85}}{SC}', clause):
                return True
            if re.search(rf'\b(?:seeking|looking for).{{0,40}}\bsc[ -]cleared\b', clause):
                return True
    return False
