import unittest

from watchdog.validation import (
    ValidationError,
    validate_http_url,
    validate_interval,
    validate_resume_prompt,
    validate_thread_id,
)


class ValidationTests(unittest.TestCase):
    def test_http_url_normalizes_trailing_slash_and_rejects_credentials(self) -> None:
        self.assertEqual(validate_http_url(" https://api.example/v1/ "), "https://api.example/v1")
        with self.assertRaises(ValidationError):
            validate_http_url("https://key:secret@api.example/v1")

    def test_thread_interval_and_prompt_have_strict_boundaries(self) -> None:
        self.assertEqual(validate_thread_id("019fa619-0c95-76c3-a151-9289b7510e09"), "019fa619-0c95-76c3-a151-9289b7510e09")
        self.assertEqual(validate_interval("5"), 5)
        self.assertEqual(validate_resume_prompt(" continue current task "), "continue current task")
        for invalid in ("not-a-uuid",):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_thread_id(invalid)
        for invalid in (0, 4, "bad"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_interval(invalid)
        for invalid in ("", " " * 3, "x" * 4001):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_resume_prompt(invalid)
