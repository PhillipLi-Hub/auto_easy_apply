"""Format known UK phone facts without asking a language model."""

import re

from .models import normalize


def phone_kind(info):
    label = normalize(info['label']).replace('-', ' ')
    autocomplete = info.get('autocomplete', '').split()
    if 'tel-country-code' in autocomplete or label in {
        'phone country code', 'mobile country code', 'mobile phone country code',
        'telephone country code', 'country calling code', 'dialling code', 'dialing code',
    }:
        return 'code'
    if any(x in autocomplete for x in ('tel', 'tel-national')) or label in {
        'phone', 'phone number', 'mobile', 'mobile number', 'mobile phone number',
        'telephone', 'telephone number', 'contact number', 'contact phone number',
    }:
        return 'number'
    return None


def uk_subscriber(phone, country=''):
    compact = re.sub(r'[\s().-]', '', phone)
    if compact.startswith(('+44', '0044')):
        digits = compact[3:] if compact.startswith('+') else compact[4:]
        digits = digits.removeprefix('0')
    elif normalize(country) in {'uk', 'gb', 'gbr', 'united kingdom', 'great britain'}:
        digits = compact.removeprefix('0')
    else:
        return None
    return digits if re.fullmatch(r'[1-9]\d{9}', digits) else None


def uk_code_option(options, saved=None):
    available = [o for o in options if not o['disabled'] and o['value']]
    if saved:
        exact = [o for o in available if normalize(o['label']) == normalize(saved)]
        if len(exact) == 1:
            option = exact[0]
            if (re.search(r'(?<!\d)(?:\+44|0044)(?!\d)', option['label'])
                    or normalize(option['value']) in {'44', '+44', '0044', 'gb', 'uk', 'gbr'}):
                return option
            return None
    # Several territories share +44. Prefer the explicitly named UK option.
    named = [o for o in available if re.search(r'\bunited kingdom\b|\bgreat britain\b', normalize(o['label']))
             or normalize(o['value']) in {'gb', 'uk', 'gbr'}]
    if len(named) == 1:
        return named[0]
    coded = [o for o in available if re.search(r'(?<!\d)(?:\+44|0044)(?!\d)', o['label'])
             or o['value'] in {'44', '+44', '0044'}]
    return coded[0] if len(coded) == 1 else None


def fill_uk_number(control, subscriber, separate_code, info):
    national, international = '0' + subscriber, '+44' + subscriber
    candidates = [subscriber, national] if separate_code else [international, national]
    hint = re.sub(r'[\s()-]', '', info.get('placeholder', ''))
    if hint.startswith('0') or (info.get('autocomplete') == 'tel-national' and not separate_code):
        candidates = [national] + candidates
    # Test HTML constraints on a detached clone before changing the live field.
    # Never truncate a number to fit maxlength or change the subscriber digits.
    for value in dict.fromkeys(candidates):
        if not control.evaluate("""(el, value) => {
            const probe = el.cloneNode(false);
            probe.value = value;
            return (el.maxLength < 0 || value.length <= el.maxLength)
                && (el.minLength < 0 || value.length >= el.minLength)
                && probe.checkValidity();
        }""", value):
            continue
        control.fill(value)
        control.blur()
        # SDUI validation can arrive after blur rather than during fill.
        control.page.wait_for_timeout(250)
        if control.evaluate("el => el.checkValidity() && el.getAttribute('aria-invalid') !== 'true'"):
            return True
    return False
