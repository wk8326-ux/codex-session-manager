import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.native_capture import NativeCaptureError, WindowsRegionCapture


ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"region"


class WindowsRegionCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.script = Path(self.directory.name) / "capture-region.ps1"
        self.script.write_text("# test capture helper", encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def service(self, runner, *, platform: str = "nt") -> WindowsRegionCapture:
        return WindowsRegionCapture(
            self.script,
            platform=platform,
            powershell_path="powershell.exe",
            runner=runner,
        )

    def test_capture_returns_png_and_cleans_its_temporary_file(self) -> None:
        observed: dict[str, object] = {}

        def runner(command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            output = Path(command[command.index("-OutputPath") + 1])
            observed["output"] = output
            output.write_bytes(PNG)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = self.service(runner).capture()

        self.assertTrue(result["captured"])
        self.assertTrue(result["image"].startswith("data:image/png;base64,"))
        self.assertEqual(
            base64.b64decode(result["image"].split(",", 1)[1]),
            PNG,
        )
        self.assertIn("-Sta", observed["command"])
        self.assertFalse(Path(observed["output"]).exists())

    def test_cancelled_selection_is_not_reported_as_an_error(self) -> None:
        service = self.service(
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=2, stdout="", stderr=""
            )
        )

        self.assertEqual(service.capture(), {"captured": False})

    def test_invalid_output_and_unsupported_platform_fail_explicitly(self) -> None:
        def invalid_runner(command, **_kwargs):
            output = Path(command[command.index("-OutputPath") + 1])
            output.write_bytes(b"not-an-image")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(NativeCaptureError, "PNG"):
            self.service(invalid_runner).capture()
        with self.assertRaisesRegex(NativeCaptureError, "Windows"):
            self.service(invalid_runner, platform="posix").capture()

    def test_helper_implements_a_native_drag_selection_overlay(self) -> None:
        source = (ROOT / "scripts" / "capture-region.ps1").read_text(
            encoding="utf-8"
        )

        for contract in (
            "SystemInformation]::VirtualScreen",
            "CopyFromScreen",
            "Add_MouseDown",
            "Add_MouseMove",
            "Add_MouseUp",
            "Keys]::Escape",
        ):
            self.assertIn(contract, source)


if __name__ == "__main__":
    unittest.main()
