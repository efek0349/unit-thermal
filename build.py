#!/usr/bin/env python3
"""
ThermalScope -> tek dosya (kurulumsuz) çalıştırılabilir.

    python build.py

Windows'ta  dist/ThermalScope.exe
Linux'ta    dist/ThermalScope           üretir.

ÖNEMLİ: Çapraz derleme yok. Windows exe'yi Windows'ta, Linux binary'yi Linux'ta
derleyin (ya da .github/workflows/build.yml ile ikisini de GitHub'a yaptırın).

ffmpeg otomatik indirilip (statik build) pakete gömülür; böylece kullanıcıda ffmpeg
kurulu olması gerekmez. Kendi ffmpeg'inizi kullanmak için vendor/ffmpeg(.exe) koyun.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
EXE = "ffmpeg.exe" if IS_WIN else "ffmpeg"
VENDOR = ROOT / "vendor"
BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
FFMPEG_URL = BASE + ("ffmpeg-master-latest-win64-gpl.zip" if IS_WIN
                     else "ffmpeg-master-latest-linux64-gpl.tar.xz")


def ensure_ffmpeg() -> None:
    target = VENDOR / EXE
    if target.is_file():
        print(f"[build] ffmpeg hazır: {target}")
        return
    VENDOR.mkdir(exist_ok=True)
    archive = VENDOR / FFMPEG_URL.rsplit("/", 1)[1]
    print(f"[build] ffmpeg indiriliyor: {FFMPEG_URL}")
    urllib.request.urlretrieve(FFMPEG_URL, archive)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            member = next(n for n in z.namelist() if n.endswith("/bin/" + EXE))
            with z.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/bin/" + EXE))
            src = t.extractfile(member)
            assert src is not None
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    target.chmod(0o755)
    archive.unlink()
    print(f"[build] ffmpeg çıkarıldı: {target} ({target.stat().st_size // 2**20} MB)")


def main() -> int:
    for need in ("ThermalScope.pyw", "uti_uvc.py", "uti256_usb.py", "palettes"):
        if not (ROOT / need).exists():
            sys.exit(f"[build] HATA: '{need}' bulunamadı. build.py, orijinal proje "
                     f"klasöründe ({ROOT}) çalıştırılmalı.")
    ensure_ffmpeg()
    sep = ";" if IS_WIN else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "ThermalScope", "--noupx",                    # UPX: yavaş açılış + antivirüs yanlış alarmı
        "--add-data", f"{ROOT / 'palettes'}{sep}palettes",      # paletler (yoksa uygulama sessizce Gray'e düşer)
        "--add-binary", f"{VENDOR / EXE}{sep}.",                # gömülü ffmpeg
        "--collect-binaries", "usb1",                           # libusb (kuruluysa)
        "--hidden-import", "uti_uvc", "--hidden-import", "uti256_usb",
    ]
    for mod in ("pygame", "qt_material", "yaml", "psutil", "tkinter", "matplotlib", "scipy", "pandas"):
        cmd += ["--exclude-module", mod]                         # projeyle ilgisiz, genel ortamdan sızanlar
    if IS_WIN:
        cmd.append("--noconsole")
        ico = ROOT / "ThermalScope.ico"
        if ico.is_file():
            cmd += ["--icon", str(ico)]
        else:
            print("[build] UYARI: ThermalScope.ico yok, varsayılan ikonla derleniyor.")
    cmd.append(str(ROOT / "ThermalScope.pyw"))
    print("[build] " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)
    out = ROOT / "dist" / ("ThermalScope.exe" if IS_WIN else "ThermalScope")
    print(f"\n[build] TAMAM -> {out}  ({out.stat().st_size // 2**20} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
