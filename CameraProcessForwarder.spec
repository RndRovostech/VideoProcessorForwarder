# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build for the Rovostech Video Processor Forwarder.
#
#   pyinstaller CameraProcessForwarder.spec
#
# What this bundles: Python, PyQt5, OpenCV, NumPy.
# What it does NOT bundle: FFmpeg and GStreamer. Both stay external programs on
# the system PATH, exactly as they are when running from source -- the app already
# locates them at runtime (see find_gstreamer_path() and shutil.which("ffmpeg")),
# and GStreamer in particular does not relocate into a bundle cleanly because its
# plugin registry expects a real install.
#
# Two deliberate choices worth knowing before changing them:
#
#   console=True   The app's whole diagnostic story is printed to stdout --
#                  [ffmpeg-rtp], [gstreamer] tags, filter tracebacks, and the
#                  "cv2.xphoto missing" warning. A windowed build discards all of
#                  it and leaves a failing pipeline with no visible explanation.
#
#   onedir         Not --onefile. OpenCV's DLLs are large; onefile re-extracts
#                  them to a temp folder on every launch, which costs seconds of
#                  startup for no benefit here.

block_cipher = None

a = Analysis(
    ['CameraProcessForwarder.py'],
    pathex=[],
    binaries=[],
    # The icon is needed twice and for different reasons: `icon=` below stamps it
    # into the .exe for Explorer and the taskbar, while this datas entry ships the
    # file itself so load_app_icon() can still find it at runtime via sys._MEIPASS.
    # Setting only `icon=` gives a correct Explorer icon and a default Qt window icon.
    datas=[('images/AppIcon.ico', 'images')],
    # cv2.xphoto is reached through hasattr(cv2, "xphoto") rather than a plain
    # import, so static analysis cannot see it. Without this the build succeeds
    # and White Balance silently does nothing -- the exact failure the readme
    # warns about, but with no pip install able to fix it.
    hiddenimports=['cv2', 'cv2.xphoto', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Insurance, not optimisation. Measured: this build is 243.4 MB with the full
    # list, with only tkinter/matplotlib, and with no excludes at all -- identical
    # every time, because PyInstaller only collects what is actually imported and
    # nothing here imports any of them. They are kept so that adding, say, a
    # matplotlib debug plot to a filter cannot quietly add hundreds of MB to the
    # shipped build. Do not expect removing them to change the size.
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'pandas',
        'PIL',
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
    [],
    exclude_binaries=True,
    name='CameraProcessForwarder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='images/AppIcon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CameraProcessForwarder',
)
