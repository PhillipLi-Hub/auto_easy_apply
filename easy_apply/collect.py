"""Collect search listings into SQLite without starting any applications."""

import threading

from .browser import LinkedInBrowser
from .models import DATA, load_settings
from .store import Store


def main():
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    lock = (DATA / 'app.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise SystemExit('Close the desktop app before running the command-line collector.')
    settings = load_settings().validate(for_run=True)
    store = Store()
    browser = LinkedInBrowser(threading.Event(), lambda message: print(message, flush=True))
    try:
        browser.open()
        # Saved browser cookies are reused; login challenges still stop the scan.
        browser.page.goto(settings.search_url(), wait_until='domcontentloaded')
        browser.discover(settings, store, full_scan=True)
        print(f"Database contains {len(store.rows())} jobs; {len(store.rows('queued'))} queued.", flush=True)
    finally:
        browser.close()
        lock.close()


if __name__ == '__main__':
    main()
