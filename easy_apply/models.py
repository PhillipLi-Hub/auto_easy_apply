import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse
from .clearance import requires_sc, REASON as SC_REJECTION
from .employment import employment_rejection
from .seniority import seniority_rejection

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def normalize(text):
    return " ".join(text.replace("*", "").split()).strip().rstrip(":?").casefold()


def job_id(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}:
        raise ValueError("Use an https://www.linkedin.com/jobs/view/... job URL")
    match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)/?$", parsed.path)
    if not match:
        raise ValueError("Job URL must contain a numeric LinkedIn job ID")
    return match.group(1)


@dataclass
class Settings:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    postcode: str = ""
    country: str = ""
    resume: str = ""
    resume_source: str = "linkedin_first"
    allow_missing_metadata: bool = False
    ai_enabled: bool = False
    unknown_experience_as_no: bool = False
    ai_provider: str = "ollama"
    local_model: str = "qwen3:4b-instruct"
    no_security_clearance: bool = False
    willing_relocate_anywhere: bool = False
    ai_model: str = "gpt-4.1-mini"
    max_ai_calls: int = 30
    cv_text: str = ""
    api_key: str = field(default="", repr=False, compare=False)
    keywords: str = ""
    location: str = ""
    title_include: str = ""
    title_exclude: str = ""
    company_exclude: str = ""
    description_require: str = ""
    remote_only: bool = False
    prioritize_quant: bool = False
    newest_first: bool = False
    exclude_sc_required: bool = False
    permanent_full_time_only: bool = False
    exclude_senior_roles: bool = False
    max_applications: int = 5
    run_until_daily_limit: bool = False
    max_results: int = 25
    answers: dict = field(default_factory=dict)

    def validate(self, for_run=False):
        if type(self.run_until_daily_limit) is not bool:
            raise ValueError('Run until daily limit must be true or false')
        if type(self.exclude_senior_roles) is not bool:
            raise ValueError('Senior and leadership role exclusion must be true or false')
        if type(self.permanent_full_time_only) is not bool:
            raise ValueError('Full-time permanent preference must be true or false')
        if type(self.exclude_sc_required) is not bool:
            raise ValueError('SC clearance filter must be true or false')
        if type(self.prioritize_quant) is not bool or type(self.newest_first) is not bool:
            raise ValueError('Job priority preferences must be true or false')
        if type(self.unknown_experience_as_no) is not bool:
            raise ValueError('Unknown experience fallback must be true or false')
        if self.ai_provider not in {'ollama', 'openai'}:
            raise ValueError('Choose Local Ollama or OpenAI as the AI provider')
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', self.local_model) or self.local_model.endswith(('-cloud', ':cloud')):
            raise ValueError('Enter an installed local Ollama model name; cloud models are not supported')
        if type(self.no_security_clearance) is not bool:
            raise ValueError('Security clearance preference must be true or false')
        if type(self.willing_relocate_anywhere) is not bool:
            raise ValueError('Relocation preference must be true or false')
        if self.resume_source not in {"linkedin_first", "local_file"}:
            raise ValueError("Choose first LinkedIn resume or local file")
        if type(self.max_ai_calls) is not int or not 1 <= self.max_ai_calls <= 100:
            raise ValueError("AI calls per run must be between 1 and 100")
        if not isinstance(self.cv_text, str) or len(self.cv_text) > 80000:
            raise ValueError("CV text must contain at most 80,000 characters")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", self.ai_model):
            raise ValueError("Enter a valid OpenAI model ID")
        if type(self.max_applications) is not int or not 1 <= self.max_applications <= 50:
            raise ValueError("Applications per run must be between 1 and 50")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 100:
            raise ValueError("Search results per scan must be between 1 and 100")
        if not isinstance(self.answers, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip() for k, v in self.answers.items()):
            raise ValueError("Saved answers must be a JSON object of nonempty question: answer strings")
        if len({normalize(k) for k in self.answers}) != len(self.answers):
            raise ValueError("Two saved questions normalize to the same label")
        if self.resume_source == "local_file" and self.resume and (not Path(self.resume).is_file() or Path(self.resume).suffix.lower() not in {".pdf", ".doc", ".docx"}):
            raise ValueError("Choose an existing PDF, DOC or DOCX resume")
        if for_run:
            if not self.first_name.strip() or not self.last_name.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
                raise ValueError("Enter your name and a valid email before starting")
            if not self.keywords.strip() or not self.location.strip():
                raise ValueError("Set job keywords and location before starting")
        return self

    def answer(self, label):
        custom = {normalize(k): v for k, v in self.answers.items()}
        key = normalize(label)
        if key in custom:
            return custom[key]
        # An explicit degree fact supports equivalent possession/completion
        # questions, but not 'PhD OR Masters', candidacy, or willingness to study.
        degree = r'(?:ph\.?\s?d\.?|doctor of philosophy)(?: degree)?'
        phd_question = (
            rf'(?:do you (?:have|hold|possess) (?:a |an )?(?:completed )?{degree}'
            rf'|have you (?:completed|earned|obtained) (?:a |an )?{degree}'
            rf'|have you completed the following level of education: {degree})'
        )
        if re.fullmatch(phd_question, key):
            return custom.get(normalize('Do you have a PhD?'))
        if self.willing_relocate_anywhere and re.search(r'\brelocat(?:e|ing|ion)\b', key):
            # Willingness does not establish timing, funding, visas, or an
            # existing residence. Exact saved answers still take priority.
            conditions = re.search(r'\b(?:not|unwilling|unable|without|own expense|cost|costs|fund|funding|assistance|support|sponsor|visa|authori[sz]|eligible|eligibility|when|how soon|within|before|immediately|weeks?|months?|days?|already|currently|and|travel|commute)\b', key)
            if not conditions and re.match(r'^(?:are|would) you (?:be )?(?:willing|happy|open|prepared|ready) (?:to |for )?relocat(?:e|ing|ion)\b', key):
                return 'Yes'
            if key in {'willing to relocate', 'open to relocation', 'relocation', 'relocation willingness'}:
                return 'Yes'
            if not conditions and re.match(r'^(?:would you consider relocating|do you (?:have a )?willingness to relocate)\b', key):
                return 'Yes'
            if not conditions and re.match(r'^where (?:are|would) you (?:be )?(?:willing|happy|open|prepared|ready) to relocate\b', key):
                return 'I am happy to relocate anywhere.'
        if self.no_security_clearance:
            clearance = re.search(r'\b(?:security|sc|dv|ctc|nppv|developed vetting)\s+clearance\b|\b(?:sc|dv|ctc|security)\s+cleared\b', key)
            # No clearance does not establish willingness or eligibility to obtain it.
            future = re.search(r'eligible|eligibility|willing|obtain|undergo|previous|ever|past|sponsor', key)
            if clearance and not future and re.search(r'^(?:do you (?:currently )?(?:hold|have)|are you (?:currently )?(?:sc|dv|ctc|security) cleared)', key):
                return 'No'
        aliases = {"first name": self.first_name, "last name": self.last_name,
                   "email": self.email, "email address": self.email,
                   "phone": self.phone, "phone number": self.phone,
                   "mobile phone number": self.phone}
        aliases.update({"full name": f"{self.first_name} {self.last_name}".strip(),
                        "address": self.address, "street address": self.address,
                        "city": self.city, "city of residence": self.city,
                        "location (city)": self.city, "city or location": self.city,
                        "postal code": self.postcode, "postcode": self.postcode,
                        "zip code": self.postcode, "country": self.country})
        return aliases.get(key) or None

    def search_url(self, past_24_hours=False):
        query = {"keywords": self.keywords, "location": self.location, "f_AL": "true"}
        if past_24_hours:
            query['f_TPR'] = 'r86400'
        if self.newest_first:
            query['sortBy'] = 'DD'
        if self.remote_only:
            query["f_WT"] = "2"
        return "https://www.linkedin.com/jobs/search/?" + urlencode(query)

    def rejection(self, job):
        if self.exclude_senior_roles:
            if not (job.get('title') or '').strip():
                return 'Cannot check role level: missing title'
            reason = seniority_rejection(job)
            if reason:
                return reason
        if self.permanent_full_time_only:
            reason = employment_rejection(job)
            if reason:
                return reason
        if self.exclude_sc_required and requires_sc(job.get('title', ''), job.get('description', '')):
            return SC_REJECTION
        def terms(value):
            return [normalize(x) for x in value.split(",") if x.strip()]
        title, company, location, description = (normalize(job.get(k, "")) for k in ["title", "company", "location", "description"])
        includes = terms(self.title_include)
        if includes and not title:
            return "Cannot check configured title filter: missing title"
        needed = {"title": bool(includes or terms(self.title_exclude)), "company": bool(terms(self.company_exclude)),
                  "location": bool(self.location), "description": bool(terms(self.description_require)),
                  "workplace": self.remote_only}
        missing = [key for key, required in needed.items() if required and not job.get(key)]
        if missing and not self.allow_missing_metadata:
            return "Cannot check configured filters: missing " + ", ".join(missing)
        if title and includes and not any(x in title for x in includes):
            return "Title does not match an included phrase"
        if any(x in title for x in terms(self.title_exclude)):
            return "Excluded title phrase"
        if any(x in company for x in terms(self.company_exclude)):
            return "Excluded company"
        if location and normalize(self.location) not in location:
            return "Job location does not contain the configured location"
        if self.remote_only and job.get("workplace") and normalize(job.get("workplace", "")) != "remote":
            return "Remote workplace was not confirmed"
        if description and not all(x in description for x in terms(self.description_require)):
            return "Missing required description phrase"
        return None


def load_settings(path=None):
    path = Path(path or DATA / "settings.json")
    return Settings(**json.loads(path.read_text(encoding="utf-8"))).validate() if path.exists() else Settings()


def save_settings(settings, path=None):
    settings.validate()
    path = Path(path or DATA / "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    values = asdict(settings)
    values.pop("api_key", None)  # Session-only credential; never write it to settings.
    temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
    temporary.replace(path)
