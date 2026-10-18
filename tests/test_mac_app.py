import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_BUNDLE = ROOT / "ClipForge.app"
CONTENTS = APP_BUNDLE / "Contents"

def test_mac_app_bundle_structure():
    assert APP_BUNDLE.is_dir(), "ClipForge.app should exist as a directory"
    assert (CONTENTS / "Info.plist").is_file(), "Info.plist must exist"
    assert (CONTENTS / "PkgInfo").is_file(), "PkgInfo must exist"
    assert (CONTENTS / "MacOS" / "ClipForge").is_file(), "Executable launcher must exist"
    assert (CONTENTS / "Resources" / "AppIcon.icns").is_file(), "AppIcon.icns must exist"

def test_info_plist_validity():
    plist_path = CONTENTS / "Info.plist"
    with plist_path.open("rb") as f:
        data = plistlib.load(f)
    assert data.get("CFBundleExecutable") == "ClipForge"
    assert data.get("CFBundleIdentifier") == "com.clipforge.app"
    assert data.get("CFBundleName") == "ClipForge"

def test_launcher_executable_bit():
    launcher = CONTENTS / "MacOS" / "ClipForge"
    assert launcher.stat().st_mode & 0o111, "Launcher should be executable"
