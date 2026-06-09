# PyInstaller spec for the standalone Stashler desktop app.
# Build:  python build.py     (generates the icon, then runs PyInstaller)
#   or:   pyinstaller --noconfirm stashler.spec
# Output: dist/Stashler.exe  (single windowed executable, tray app)

block_cipher = None

# Bundle the Flask templates/static and the packaged default rules, preserving the
# package layout so create_app()/tray.py find them under sys._MEIPASS at runtime.
datas = [
    ("stasher/ui/templates", "stasher/ui/templates"),
    ("stasher/ui/static", "stasher/ui/static"),
    ("stasher/evaluate/default_rules.toml", "stasher/evaluate"),
    ("stasher/evaluate/default.filter", "stasher/evaluate"),
    ("stasher/evaluate/default_archetype_set.yaml", "stasher/evaluate"),
]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    # pystray picks its backend dynamically; pin the Windows one so it's bundled.
    hiddenimports=["pystray._win32"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
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
    name="Stashler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,           # windowed: no terminal, just the tray icon
    icon="stashler.ico",
)
