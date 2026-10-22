"""Import a user-supplied CV locally without opening a browser or making API calls."""

import argparse
import shutil
from datetime import datetime

from .cv import contact_details, read_cv
from .models import DATA, load_settings, save_settings
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cv")
    parser.add_argument("--first-linkedin-resume", action="store_true")
    parser.add_argument("--enable-ai", action="store_true")
    parser.add_argument("--allow-missing-metadata", action="store_true")
    parser.add_argument("--requeue-metadata-failures", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    settings.cv_text = read_cv(args.cv)
    settings.resume = args.cv
    for key, value in contact_details(settings.cv_text).items():
        if not getattr(settings, key).strip():
            setattr(settings, key, value)
    if args.first_linkedin_resume:
        settings.resume_source = "linkedin_first"
    if args.enable_ai:
        settings.ai_enabled = True
    if args.allow_missing_metadata:
        settings.allow_missing_metadata = True
    current = DATA / "settings.json"
    if current.exists():
        backup = DATA / ("settings.before_cv_import." + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
        shutil.copy2(current, backup)
    save_settings(settings)
    count = 0
    if args.requeue_metadata_failures:
        store = Store()
        for row in store.rows("skipped"):
            if row["note"] == "Could not reliably read title, company, location and description":
                count += store.requeue(row["id"])
    print(f"Imported {len(settings.cv_text)} CV characters locally. Requeued {count} metadata failures. Restart the app to load these settings.")


if __name__ == "__main__":
    main()
