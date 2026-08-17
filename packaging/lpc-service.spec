# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).resolve().parent
resources = [
    (str(project_root / "index.html"), "."),
    (str(project_root / "watchdog.html"), "."),
    (str(project_root / "remote.html"), "."),
    (str(project_root / "remote.css"), "."),
    (str(project_root / "remote.js"), "."),
    (str(project_root / "remote-setup.html"), "."),
    (str(project_root / "remote-setup.css"), "."),
    (str(project_root / "remote-setup.js"), "."),
    (str(project_root / "manifest.webmanifest"), "."),
    (str(project_root / "service-worker.js"), "."),
    (str(project_root / "assets"), "assets"),
    (str(project_root / "scripts" / "setup-remote-client.ps1"), "scripts"),
]

analysis = Analysis(
    [str(project_root / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=resources,
    hiddenimports=collect_submodules("watchdog") + collect_submodules("remote"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="lpc-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "assets" / "project-console-brand.ico"),
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="lpc-service",
)
