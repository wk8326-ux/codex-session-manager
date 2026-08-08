import base64
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from remote.native_capture import FlameshotRegionCapture, NativeCaptureError


PNG = b"\x89PNG\r\n\x1a\n" + b"region"


class FlameshotRegionCaptureTests(unittest.TestCase):
    def service(
        self,
        runner,
        *,
        platform: str = "nt",
        executable_path: str = r"C:\Program Files\Flameshot\bin\flameshot.exe",
    ) -> FlameshotRegionCapture:
        return FlameshotRegionCapture(
            platform=platform,
            executable_path=executable_path,
            runner=runner,
        )

    def test_capture_reads_flameshot_png_file_and_cleans_it_up(self) -> None:
        observed: dict[str, object] = {}

        def runner(command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            output = Path(command[command.index("--path") + 1])
            observed["output"] = output
            output.write_bytes(PNG)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        result = self.service(runner).capture()

        self.assertTrue(result["captured"])
        self.assertEqual(
            base64.b64decode(result["image"].split(",", 1)[1]),
            PNG,
        )
        self.assertEqual(observed["command"][1], "gui")
        self.assertEqual(observed["command"][2], "--path")
        self.assertEqual(observed["command"][-1], "--accept-on-select")
        self.assertNotIn("--raw", observed["command"])
        self.assertEqual(observed["kwargs"]["stdout"], subprocess.PIPE)
        self.assertEqual(observed["kwargs"]["stderr"], subprocess.PIPE)
        self.assertNotIn("text", observed["kwargs"])
        self.assertFalse(Path(observed["output"]).exists())

    def test_empty_successful_capture_is_treated_as_cancelled(self) -> None:
        service = self.service(
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout=b"", stderr=b""
            )
        )

        self.assertEqual(service.capture(), {"captured": False})

    def test_flameshot_aborted_exit_is_treated_as_cancelled(self) -> None:
        service = self.service(
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=2,
                stdout=b"",
                stderr=b"flameshot: info: Screenshot aborted.",
            )
        )

        self.assertEqual(service.capture(), {"captured": False})

    def test_invalid_output_and_unsupported_platform_fail_explicitly(self) -> None:
        def invalid_runner(command, **_kwargs):
            output = Path(command[command.index("--path") + 1])
            output.write_bytes(b"not-an-image")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with self.assertRaisesRegex(NativeCaptureError, "PNG"):
            self.service(invalid_runner).capture()
        with self.assertRaisesRegex(NativeCaptureError, "Windows"):
            self.service(invalid_runner, platform="posix").capture()

    def test_missing_flameshot_has_an_install_instruction(self) -> None:
        with patch("remote.native_capture.shutil.which", return_value=None), patch.dict(
            os.environ,
            {
                "PROGRAMFILES": r"C:\missing",
                "LOCALAPPDATA": r"C:\also-missing",
            },
            clear=False,
        ), patch.object(Path, "is_file", return_value=False):
            service = FlameshotRegionCapture(platform="nt", runner=lambda: None)

            with self.assertRaisesRegex(
                NativeCaptureError, "install-screenshot-tool.bat"
            ):
                service.capture()


if __name__ == "__main__":
    unittest.main()
