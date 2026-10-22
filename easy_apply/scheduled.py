"""Windows Task Scheduler entry point; the single desktop owns browser work."""
from .store import Store
from .schedule import ScheduleQueue, schedule_due


def main(if_due=False):
    store = Store()
    if if_due and not schedule_due(store.path.parent):
        print('Not due: waiting for the enabled daily start time.')
        return
    schedule = ScheduleQueue(store)
    day, created = schedule.request()
    if not created and not schedule.pending(day):
        print(f'Scheduled request for {day} already exists; no duplicate run started.')
        return
    from .desktop import main as desktop_main
    desktop_main(quiet_if_open=True)


if __name__ == '__main__':
    import argparse
    import sys
    import traceback
    from datetime import datetime
    from .models import DATA
    parser = argparse.ArgumentParser()
    parser.add_argument('--if-due', action='store_true')
    args = parser.parse_args()
    directory = DATA / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f'scheduled-{datetime.now():%Y-%m-%d}.log').open('a', encoding='utf-8', buffering=1) as log:
        sys.stdout = sys.stderr = log
        print(f'{datetime.now().astimezone().isoformat(timespec="seconds")} Scheduler invoked')
        try:
            main(if_due=args.if_due)
        except Exception:
            traceback.print_exc()
            raise SystemExit(1)
