import io
import json
import unittest
from unittest.mock import patch

from easy_apply.local_llm import generate, LocalModelError
from easy_apply.models import Settings


class LocalModelTests(unittest.TestCase):
    @patch('easy_apply.local_llm.ensure_server')
    @patch('easy_apply.local_llm.open_local')
    def test_stream_uses_schema_and_waits_for_completion(self, request, ensure):
        events = [{'message': {'content': '{"answer":'}},
                  {'message': {'content': '"Python"}'}, 'done': True, 'done_reason': 'stop', 'eval_count': 8}]
        request.return_value = io.BytesIO(('\n'.join(json.dumps(e) for e in events)).encode())
        response = generate('qwen3:4b-instruct', 'Follow CV facts', 'question', {'type': 'object'})
        self.assertEqual(response['output'][0]['content'][0]['text'], '{"answer":"Python"}')
        self.assertEqual(request.call_args.args[0], '/api/chat')
        self.assertEqual(request.call_args.args[1]['format'], {'type': 'object'})
        self.assertTrue(request.call_args.args[1]['stream'])

    @patch('easy_apply.local_llm.ensure_server')
    @patch('easy_apply.local_llm.open_local')
    def test_incomplete_or_length_limited_answer_is_not_accepted(self, request, ensure):
        for event in [{'message': {'content': 'partial'}}, {'done': True, 'done_reason': 'length'}]:
            request.return_value = io.BytesIO(json.dumps(event).encode())
            with self.assertRaisesRegex(LocalModelError, 'incomplete'):
                generate('qwen3:4b-instruct', '', '', {})

    @patch('easy_apply.local_llm.ensure_server')
    @patch('easy_apply.local_llm.open_local')
    def test_stop_during_stream_does_not_return_an_answer(self, request, ensure):
        request.return_value = io.BytesIO(b'{"message":{"content":"partial"}}\n')
        def stop():
            raise RuntimeError('Stopped by user')
        with self.assertRaisesRegex(RuntimeError, 'Stopped by user'):
            generate('qwen3:4b-instruct', '', '', {}, stop)

    def test_cloud_model_and_unknown_provider_are_rejected(self):
        for settings in [Settings(local_model='some-model:cloud'), Settings(ai_provider='unknown')]:
            with self.assertRaises(ValueError):
                settings.validate()
