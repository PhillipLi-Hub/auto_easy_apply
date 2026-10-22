"""Project-local Ollama runtime. Only loopback inference; no paid fallback."""

import json
import os
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .models import DATA

BASE_URL = 'http://127.0.0.1:11435'
RUNTIME = DATA / 'ollama' / 'runtime' / 'ollama.exe'


class LocalModelError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalModelError('Local AI endpoint attempted a redirect; request stopped')


def open_local(path, payload=None, timeout=60):
    request = Request(BASE_URL + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)


def ensure_server(check=lambda: None):
    check()
    try:
        with open_local('/api/version', timeout=1) as response:
            json.load(response)
        return
    except (URLError, OSError):
        pass
    if not RUNTIME.is_file():
        raise LocalModelError('Local AI is not installed. Run SetupLocalAI.ps1 first.')
    env = os.environ.copy()
    env.update(OLLAMA_HOST='127.0.0.1:11435', OLLAMA_MODELS=str(DATA / 'ollama' / 'models'),
               OLLAMA_NO_CLOUD='1', OLLAMA_NUM_PARALLEL='1', OLLAMA_CONTEXT_LENGTH='8192',
               OLLAMA_DEBUG='0')
    with (DATA / 'ollama' / 'server.log').open('ab') as log:
        subprocess.Popen([str(RUNTIME), 'serve'], env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(80):
        check()
        try:
            with open_local('/api/version', timeout=1) as response:
                json.load(response)
            return
        except (URLError, OSError):
            time.sleep(.25)
    raise LocalModelError('Local AI did not start; check data/ollama/server.log')


def generate(model, instructions, context, schema, check=lambda: None):
    ensure_server(check)
    payload = {'model': model, 'stream': True, 'format': schema, 'keep_alive': '5m',
               'messages': [{'role': 'system', 'content': instructions +
                             '\nAnswer only the specific question, not an entire CV skills list. '
                             'For programming-language questions, include only programming/query languages supported by the CV; '
                             'exclude frameworks, libraries, products, platforms and tools such as Docker, AWS, Git, PyTorch or Databricks. '
                             'Return JSON matching this schema: ' + json.dumps(schema)},
                            {'role': 'user', 'content': context}],
               'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 1200}}
    pieces, last = [], {}
    try:
        with open_local('/api/chat', payload) as response:
            for line in response:
                check()
                if not line.strip():
                    continue
                last = json.loads(line)
                if last.get('error'):
                    raise LocalModelError('Local model could not generate an answer; check the server log')
                pieces.append(last.get('message', {}).get('content', ''))
                if last.get('done'):
                    break
    except HTTPError as exc:
        if exc.code == 404:
            raise LocalModelError(f'Local model {model} is not installed. Run SetupLocalAI.ps1.') from None
        raise LocalModelError(f'Local model returned HTTP {exc.code}; check data/ollama/server.log') from None
    except (URLError, OSError, ValueError):
        raise LocalModelError('Local model request failed or timed out; no answer was filled') from None
    if not last.get('done') or last.get('done_reason') == 'length':
        raise LocalModelError('Local model response was incomplete; no answer was filled')
    return {'status': 'completed', 'id': None, 'model': last.get('model', model),
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': ''.join(pieces)}]}],
            'usage': {'input_tokens': last.get('prompt_eval_count'), 'output_tokens': last.get('eval_count')}}
