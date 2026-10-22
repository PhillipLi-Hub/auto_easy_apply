"""Download a local model after the verified runtime has been installed."""
import argparse
import json
from .local_llm import ensure_server, open_local
from .models import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='qwen3:4b-instruct')
    args = parser.parse_args()
    Settings(local_model=args.model).validate()
    ensure_server()
    previous = None
    with open_local('/api/pull', {'model': args.model, 'stream': True}, timeout=120) as response:
        for line in response:
            event = json.loads(line)
            if event.get('error'):
                raise RuntimeError(event['error'])
            status = event.get('status', '')
            if event.get('total'):
                status += f" {int(event.get('completed', 0) / event['total'] * 10) * 10}%"
            if status != previous:
                print(status, flush=True)
                previous = status
    if previous != 'success':
        raise RuntimeError('Model download did not finish successfully')
    print(f'Local model ready: {args.model}', flush=True)


if __name__ == '__main__':
    main()
