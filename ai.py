"""Ella's brain: Anthropic's Claude, or OpenAI.

Every part of E&L talks to the model the same way (client.messages.create / client.messages.stream, the Anthropic
shape), so switching is two settings in Render, and nothing else changes:

  AI_PROVIDER=openai        use OpenAI (with OPENAI_API_KEY). Unset: Claude, unless only OPENAI_API_KEY is set.
  OPENAI_API_KEY            from platform.openai.com (a ChatGPT Pro plan's monthly API credits land there)
  OPENAI_STYLE_MODEL        writes looks, stories, reads photos   (default gpt-6.1-terra)
  OPENAI_QUICK_MODEL        small fast jobs                       (default gpt-6-luna)
  OPENAI_REASONING          how hard the style model thinks: none / low / medium / high (default low)

OpenAIClient answers like the Anthropic client: .content[0].text, .usage.input_tokens / .output_tokens, and a stream
with .text_stream and .get_final_message(). Pictures in the Anthropic shape (base64 or a URL) are passed on.
Errors carry .status_code and .body, so alerts.py spots running out of credits or a refused key the same way.
"""
import json
import os
import random
import time
import types

import requests as req

API = 'https://api.openai.com/v1/chat/completions'
STYLE_DEFAULT = 'gpt-6.1-terra'
QUICK_DEFAULT = 'gpt-6-luna'
REASONING_ROOM = 4000      # thinking tokens count toward the output limit: room for them on top of the answer


def provider():
    chosen = (os.environ.get('AI_PROVIDER') or '').strip().lower()
    if chosen in ('openai', 'anthropic'):
        return chosen
    return 'openai' if os.environ.get('OPENAI_API_KEY') and not os.environ.get('ANTHROPIC_API_KEY') else 'anthropic'


def make_client():
    if provider() == 'openai':
        return OpenAIClient(os.environ.get('OPENAI_API_KEY'))
    import anthropic
    return anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))


def openai_model(model):
    """The Claude model a part of E&L asks for -> the OpenAI model that does that job."""
    m = (model or '').lower()
    if m and not m.startswith('claude'):
        return model                        # already an OpenAI model
    if 'haiku' in m:
        return os.environ.get('OPENAI_QUICK_MODEL') or QUICK_DEFAULT
    return os.environ.get('OPENAI_STYLE_MODEL') or STYLE_DEFAULT


def _effort(model):
    quick = model == (os.environ.get('OPENAI_QUICK_MODEL') or QUICK_DEFAULT)
    effort = (os.environ.get('OPENAI_QUICK_REASONING' if quick else 'OPENAI_REASONING') or ('none' if quick else 'low')).lower()
    return effort if effort in ('none', 'low', 'medium', 'high', 'xhigh', 'max') else 'low'


def _part(block):
    """One Anthropic content block -> an OpenAI content part."""
    if block.get('type') == 'text':
        return {'type': 'text', 'text': block.get('text', '')}
    if block.get('type') == 'image':
        src = block.get('source') or {}
        if src.get('type') == 'base64':
            url = f"data:{src.get('media_type') or 'image/jpeg'};base64,{src.get('data', '')}"
        else:
            url = src.get('url', '')
        return {'type': 'image_url', 'image_url': {'url': url}}
    return {'type': 'text', 'text': str(block.get('text') or '')}


def _messages(messages, system=None):
    out = []
    if system:
        out.append({'role': 'system', 'content': system if isinstance(system, str) else
                    ' '.join(b.get('text', '') for b in system if isinstance(b, dict))})
    for m in messages:
        c = m.get('content')
        out.append({'role': m.get('role', 'user'), 'content': c if isinstance(c, str) else [_part(b) for b in c]})
    return out


class OpenAIError(Exception):
    """An OpenAI error with what alerts.py looks at: .status_code and .body ({'error': {type, code, message}})."""

    def __init__(self, status_code, body):
        self.status_code, self.body = status_code, body if isinstance(body, dict) else {}
        err = self.body.get('error') if isinstance(self.body.get('error'), dict) else {}
        super().__init__(f"OpenAI {status_code}: {err.get('code') or err.get('type') or ''} {err.get('message') or body}"[:600])


def _usage(u):
    u = u or {}
    cached = ((u.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
    return types.SimpleNamespace(input_tokens=max(0, (u.get('prompt_tokens') or 0) - cached),
                                 output_tokens=u.get('completion_tokens') or 0,
                                 cache_creation_input_tokens=0, cache_read_input_tokens=cached)


def _message(text, model, usage, stop):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type='text', text=text)], model=model,
                                 usage=_usage(usage), stop_reason='max_tokens' if stop == 'length' else 'end_turn')


class _Stream:
    def __init__(self, messages, a, kw):
        self._m, self._a, self._kw = messages, a, kw
        self._chunks, self._usage, self._stop, self._r, self._model = [], None, None, None, ''

    def __enter__(self):
        body = self._m._body(*self._a, **self._kw)
        self._model = body['model']
        self._r = self._m._post(dict(body, stream=True, stream_options={'include_usage': True}), stream=True)
        return self

    def __exit__(self, et, ev, tb):
        if self._r is not None:
            self._r.close()
        return False

    @property
    def text_stream(self):
        for line in self._r.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if ev.get('error'):
                raise OpenAIError(500, ev)
            if ev.get('usage'):
                self._usage = ev['usage']
            for ch in ev.get('choices') or []:
                self._stop = ch.get('finish_reason') or self._stop
                text = (ch.get('delta') or {}).get('content')
                if text:
                    self._chunks.append(text)
                    yield text

    def get_final_message(self):
        return _message(''.join(self._chunks), self._model, self._usage, self._stop)


class _Messages:
    def __init__(self, key):
        self.key = key

    def _body(self, model=None, max_tokens=1024, messages=(), system=None, temperature=None, **_):
        m = openai_model(model)
        effort = _effort(m)
        body = {'model': m, 'messages': _messages(messages, system), 'reasoning_effort': effort,
                'max_completion_tokens': int(max_tokens) + (0 if effort == 'none' else REASONING_ROOM)}
        if temperature is not None and effort == 'none':
            body['temperature'] = temperature        # only without reasoning
        return body

    def _post(self, body, stream=False):
        """POST with the retries the Anthropic client would do: busy / rate-limited / a dropped connection. Running
        out of credits or a refused key is never retried."""
        if not self.key:
            raise OpenAIError(401, {'error': {'type': 'authentication_error', 'message': 'OPENAI_API_KEY is not set'}})
        for attempt in range(3):
            try:
                r = req.post(API, json=body, stream=stream, timeout=(10, 180),
                             headers={'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
            except (req.ConnectionError, req.Timeout) as e:
                if attempt == 2:
                    raise OpenAIError(503, {'error': {'type': 'connection_error', 'message': repr(e)[:200]}}) from e
                time.sleep(1.5 * (attempt + 1) + random.random())
                continue
            if r.status_code == 200:
                return r
            try:
                err = r.json()
            except ValueError:
                err = {'error': {'message': r.text[:300]}}
            code = str(((err.get('error') or {}) if isinstance(err.get('error'), dict) else {}).get('code') or '')
            busy = r.status_code in (500, 502, 503, 504) or (r.status_code == 429 and code not in (
                'insufficient_quota', 'billing_hard_limit_reached', 'billing_not_active'))
            if busy and attempt < 2:
                r.close()
                wait = r.headers.get('retry-after')
                time.sleep(min(float(wait), 10) if wait and wait.replace('.', '', 1).isdigit() else 1.5 * (attempt + 1) + random.random())
                continue
            raise OpenAIError(r.status_code, err)

    def create(self, *a, **kw):
        body = self._body(*a, **kw)
        d = self._post(body).json()
        ch = (d.get('choices') or [{}])[0]
        return _message((ch.get('message') or {}).get('content') or '', d.get('model') or body['model'],
                        d.get('usage'), ch.get('finish_reason'))

    def stream(self, *a, **kw):
        return _Stream(self, a, kw)


class OpenAIClient:
    """Answers like anthropic.Anthropic(): client.messages.create(...) and client.messages.stream(...)."""

    def __init__(self, api_key):
        self.messages = _Messages(api_key)
