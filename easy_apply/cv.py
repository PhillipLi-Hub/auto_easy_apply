"""Local PDF/text extraction. Document contents are facts, never instructions."""

import re
from pathlib import Path


def read_cv(path):
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Use an unencrypted CV PDF")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError("Import a text-based PDF or UTF-8 text file for AI context")
    text = text.strip()
    if len(text) < 100:
        raise ValueError("Could not extract enough CV text; paste the text in the AI answers tab")
    if len(text) > 80000:
        raise ValueError("CV exceeds 80,000 characters; provide a shorter version")
    return text


def contact_details(text):
    result = {}
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone = re.search(r"(?m)^\s*(\+\d[\d ()-]{8,20}\d)(?=\s|$)", text)
    if email:
        result["email"] = email.group()
    if phone:
        result["phone"] = phone.group(1).strip()
    first_line = text.splitlines()[0].strip()
    if re.fullmatch(r"[A-Za-z'-]+(?: [A-Za-z'-]+){1,3}", first_line):
        first, last = first_line.rsplit(" ", 1)
        result.update(first_name=first.title(), last_name=last.title())
    return result
