from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CAPTURE_BYTES = 12_000_000


class NativeCaptureError(RuntimeError):
    pass


class FlameshotRegionCapture:
    def __init__(
        self,
        *,
        timeout_seconds: int = 120,
        platform: str | None = None,
        executable_path: str | Path | None = None,
        runner: Callable[..., object] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.platform = platform or os.name
        self.executable_path = str(executable_path) if executable_path else ""
        self.runner = runner or subprocess.run

    def _resolve_executable(self) -> str:
        if self.executable_path:
            return self.executable_path

        override = os.environ.get("LPC_FLAMESHOT_PATH", "").strip()
        candidates = [
            override,
            shutil.which("flameshot.exe") or "",
            shutil.which("flameshot") or "",
            str(
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
                / "Flameshot"
                / "bin"
                / "flameshot.exe"
            ),
            str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs"
                / "Flameshot"
                / "bin"
                / "flameshot.exe"
            ),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        raise NativeCaptureError(
            "未安装 Flameshot 截图组件，请运行 install-screenshot-tool.bat。"
        )

    def capture(self) -> dict:
        if self.platform != "nt":
            raise NativeCaptureError("Flameshot 区域截图当前仅支持 Windows。")
        executable = self._resolve_executable()
        with tempfile.TemporaryDirectory(prefix="lpc-flameshot-") as directory:
            output_path = Path(directory) / "capture.png"
            command = [
                executable,
                "gui",
                "--path",
                str(output_path),
                "--accept-on-select",
            ]
            try:
                result = self.runner(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise NativeCaptureError("区域截图等待超时，请重新截取。") from error
            except OSError as error:
                raise NativeCaptureError(
                    "Flameshot 无法启动，请重新运行截图组件安装脚本。"
                ) from error

            returncode = int(getattr(result, "returncode", 1))
            stderr = bytes(getattr(result, "stderr", b"") or b"").decode(
                "utf-8", errors="replace"
            )
            if not output_path.is_file() and (
                returncode == 0 or "Screenshot aborted" in stderr
            ):
                return {"captured": False}
            if returncode != 0:
                raise NativeCaptureError("Flameshot 截图未完成，请重试。")
            try:
                image = output_path.read_bytes()
            except OSError as error:
                raise NativeCaptureError("Flameshot 截图文件无法读取。") from error
            if not image.startswith(PNG_SIGNATURE):
                raise NativeCaptureError("Flameshot 没有返回有效的 PNG 图片。")
            if len(image) > MAX_CAPTURE_BYTES:
                raise NativeCaptureError("截取区域过大，请缩小范围后重试。")

        encoded = base64.b64encode(image).decode("ascii")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return {
            "captured": True,
            "image": f"data:image/png;base64,{encoded}",
            "name": f"flameshot-{timestamp}.png",
        }
