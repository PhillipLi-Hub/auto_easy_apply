import threading
import unittest
from unittest.mock import Mock, patch

from easy_apply.browser import LinkedInBrowser, Stopped
from easy_apply.limits import DailyLimit, RateLimited, classify_limit, cooldown_status


class LimitTests(unittest.TestCase):
    def test_daily_limit_takes_priority(self):
        for message in ["You've reached your daily application limit.",
                        "You reached today’s Easy Apply limit",
                        "You reached today's Easy Apply limit",
                        "Daily maximum reached for applications.",
                        "You have exceeded the Easy Apply limit for today."]:
            with self.subTest(message=message):
                self.assertIsInstance(classify_limit(["You are applying too fast", message]), DailyLimit)

    def test_temporary_limit_respects_longer_requested_wait(self):
        for text in ["You're applying too fast", "You are applying too quickly",
                     "We limit how often you can apply for jobs", "Too many applications. Try later"]:
            self.assertEqual(classify_limit([text]).seconds, 600)
        self.assertEqual(classify_limit(["You're applying too fast. Try again in 30 minutes."]).seconds, 1800)
        self.assertEqual(classify_limit(["You're applying too fast. Wait 2 hours."]).seconds, 7200)
        self.assertIsNone(classify_limit(["Application sent", "Daily salary: GBP 300", "Apply today"]))

    def test_temporarily_paused_heading_and_fast_pace_message(self):
        for message in ['Easy Apply is temporarily paused',
                        'We noticed you’re applying at a fast pace. You can continue shortly.',
                        'We have briefly paused Easy Apply. You are applying at a fast pace.']:
            with self.subTest(message=message):
                limit = classify_limit([message])
                self.assertIsInstance(limit, RateLimited)
                self.assertEqual(limit.seconds, 600)
        self.assertEqual(classify_limit(['Easy Apply is temporarily paused. Wait 20 minutes.']).seconds, 1200)
        self.assertIsInstance(classify_limit(['Easy Apply is temporarily paused', "You reached today's Easy Apply limit"]), DailyLimit)
        self.assertIsNone(classify_limit(['We work at a fast pace', 'Application paused by user']))

    def test_second_by_second_countdown_and_check_after_wait(self):
        tick = [0]
        event = Mock()
        event.wait.side_effect = lambda seconds: tick.__setitem__(0, tick[0] + seconds)
        callback = Mock()
        driver = LinkedInBrowser(event, Mock(), cooldown=callback)
        driver.check = Mock()
        with patch('easy_apply.browser.time.monotonic', side_effect=lambda: tick[0]), patch('easy_apply.browser.visible_limit', return_value=None):
            driver.pause_for_limit(RateLimited('Easy Apply is temporarily paused', seconds=3))
        self.assertEqual([call.args[0] for call in callback.call_args_list], [3, 3, 2, 1, 0])
        self.assertEqual(tick[0], 3)
        self.assertIn('10:00', cooldown_status(600))
        self.assertIn('09:59', cooldown_status(599))
        self.assertIn('checking LinkedIn', cooldown_status(0))

    def test_cooldown_waits_and_remains_interruptible(self):
        driver = LinkedInBrowser(threading.Event(), Mock())
        driver.page = Mock()
        driver.check = Mock()
        driver.stop_event = Mock()
        with patch("easy_apply.browser.time.monotonic", side_effect=[0, 0, 0, 1]), patch("easy_apply.browser.visible_limit", return_value=None):
            driver.pause_for_limit(RateLimited("Applying too fast", seconds=1))
        driver.stop_event.wait.assert_called_once_with(1)
        driver.check.assert_called_with(check_limits=False)

        event = threading.Event()
        event.set()
        driver = LinkedInBrowser(event, Mock())
        with self.assertRaises(Stopped):
            driver.pause_for_limit(RateLimited("Applying too fast"))

    def test_daily_limit_during_cooldown_stops_wait(self):
        driver = LinkedInBrowser(threading.Event(), Mock())
        driver.check = Mock()
        with patch("easy_apply.browser.visible_limit", return_value=DailyLimit("Daily maximum reached")):
            with self.assertRaises(DailyLimit):
                driver.pause_for_limit(RateLimited("Applying too fast"))
