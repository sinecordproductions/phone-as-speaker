# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller --noconfirm phonespeaker.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
# These packages carry data files or native parts that PyInstaller can miss on its own.
for pkg in ("customtkinter", "aiortc", "av", "aioice", "pylibsrtp", "google_crc32c", "pyaudiowpatch"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # a missing optional package should not stop the build
        print(f"collect_all skipped {pkg}: {e}")
hiddenimports += collect_submodules("pystray")  # picks the Windows tray backend

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # folder build, starts faster at boot than a single file
    name="PhoneSpeaker",
    console=False,           # no black terminal window
    icon="icon.ico",
    upx=False,               # UPX makes antivirus warnings more likely
)
coll = COLLECT(exe, a.binaries, a.datas, name="PhoneSpeaker", upx=False)
