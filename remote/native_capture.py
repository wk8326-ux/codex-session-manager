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


class WindowsRegionCapture:
    def __init__(
        self,
        script_path: Path,
        *,
        timeout_seconds: int = 120,
        platform: str | None = None,
        powershell_path: str | None = None,
        runner: Callable[..., object] | None = None,
    ) -> None:
        self.script_path = Path(script_path)
        self.timeout_seconds = timeout_seconds
        self.platform = platform or os.name
        self.powershell_path = powershell_path
        self.runner = runner or subprocess.run

    def capture(self) -> dict:
        if self.platform != "nt":
            raise NativeCaptureError("原生区域截图仅支持 Windows。")
        if not self.script_path.is_file():
            raise NativeCaptureError("区域截图脚本不存在，请重新安装控制台。")
        powershell = self.powershell_path or shutil.which("powershell.exe")
        if not powershell:
            raise NativeCaptureError("未找到 Windows PowerShell，无法启动区域截图。")

        with tempfile.TemporaryDirectory(prefix="lpc-region-capture-") as directory:
            output_path = Path(directory) / "capture.png"
            command = [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script_path.resolve()),
                "-OutputPath",
                str(output_path),
            ]
            try:
                result = self.runner(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise NativeCaptureError("区域截图等待超时，请重新截取。") from error
            except OSError as error:
                raise NativeCaptureError("区域截图程序无法启动。") from error

            returncode = int(getattr(result, "returncode", 1))
            if returncode == 2:
                return {"captured": False}
            if returncode != 0:
                raise NativeCaptureError("区域截图未完成，请重试。")
            try:
                image = output_path.read_bytes()
            except OSError as error:
                raise NativeCaptureError("区域截图结果无法读取。") from error
            if not image.startswith(PNG_SIGNATURE):
                raise NativeCaptureError("区域截图没有生成有效的 PNG 图片。")
            if len(image) > MAX_CAPTURE_BYTES:
                raise NativeCaptureError("截取区域过大，请缩小范围后重试。")

            encoded = base64.b64encode(image).decode("ascii")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            return {
                "captured": True,
                "image": f"data:image/png;base64,{encoded}",
                "name": f"region-{timestamp}.png",
            }
