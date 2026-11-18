# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口。

用法（在本目录下）：
    python -m PyInstaller epub_checker.spec --noconfirm
产物：dist\\EPUB检查器.exe
"""

block_cipher = None

a = Analysis(
    ["epub_checker_app.py"],
    pathex=[],
    binaries=[],
    datas=[("app.ico", ".")],
    hiddenimports=["epub_core", "epub_parse", "epub_check", "epub_edit", "epub_gui_qt", "epub_i18n"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtNetwork",
        "PySide6.QtOpenGL",
        "PySide6.QtDBus",
        "PySide6.QtDesigner",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="EPUB检查器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon="app.ico",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
