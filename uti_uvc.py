#!/usr/bin/env python3
"""
uti_uvc.py - libusb (python-libusb1) üzerinden UVC video akışı + vendor komutları.

AMAÇ: Windows'ta kameranın sürücüsü libusbK/WinUSB yapıldığında DirectShow/ffmpeg
çalışmaz. Bu modül video akışını doğrudan USB'den alır ve AYNI libusb tutamacıyla
uti256_usb.py'deki vendor komutlarını (kalibrasyon) göndermeyi sağlar.

DONANIMDA TEST EDİLMEDİ. Mantık sentetik tanımlayıcı/akış verisiyle doğrulandı
(python uti_uvc.py --selftest). İlk gerçek denemede şunları çalıştırın:
    python uti_uvc.py --list
    python uti_uvc.py --probe  VID:PID      (çıktıyı kaydedin)
    python uti_uvc.py --vendor-test VID:PID
    python uti_uvc.py --grab   VID:PID 50
    python uti_uvc.py --grabraw VID:PID 20   (iki kanalı da kaydeder)

Kurulum:  pip install libusb1 numpy
Sürücü :  Zadig -> Options -> List All Devices -> kameranın TÜM arayüzlerini
          (Interface 0 ve Interface 1 ...) libusbK yapın. Geri dönüş için README
          notlarına bakın (Aygıt Yöneticisi -> sürücüyü kaldır -> yeniden tak).
"""
from __future__ import annotations

import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

try:
    import usb1
except Exception:          # libusb1 kurulu değil
    usb1 = None

SENSOR_W, SENSOR_H = 256, 192
STREAM_W, STREAM_H = 256, 384

UVC_SET_CUR, UVC_GET_CUR, UVC_GET_LEN = 0x01, 0x81, 0x85
VS_PROBE, VS_COMMIT = 0x01, 0x02
CS_INTERFACE = 0x24
FMT_UNCOMPRESSED, FMT_MJPEG, FMT_FRAME_BASED = 0x04, 0x06, 0x10
FRM_UNCOMPRESSED, FRM_MJPEG, FRM_FRAME_BASED = 0x05, 0x07, 0x11
EP_ISO, EP_BULK = 1, 2


# ==========================================================================
# Tanımlayıcı (descriptor) ayrıştırma
# ==========================================================================
@dataclass
class FrameDesc:
    index: int
    width: int
    height: int
    max_frame_size: int
    default_interval: int
    intervals: List[int] = field(default_factory=list)
    continuous: Optional[Tuple[int, int, int]] = None


@dataclass
class FormatDesc:
    index: int
    subtype: int
    guid: bytes
    frames: List[FrameDesc] = field(default_factory=list)


@dataclass
class AltSetting:
    number: int
    endpoint: Optional[int] = None
    ep_type: Optional[int] = None
    ep_packet: int = 0               # çarpan dahil etkin paket boyutu


@dataclass
class StreamIface:
    number: int
    formats: Dict[int, FormatDesc] = field(default_factory=dict)
    alts: List[AltSetting] = field(default_factory=list)
    control_iface: Optional[int] = None


def parse_vs_extra(extra: bytes) -> Dict[int, FormatDesc]:
    formats: Dict[int, FormatDesc] = {}
    cur: Optional[FormatDesc] = None
    i, n = 0, len(extra)
    while i + 3 <= n:
        ln = extra[i]
        if ln < 3 or i + ln > n:
            break
        d = extra[i:i + ln]
        if d[1] == CS_INTERFACE:
            st = d[2]
            if st in (FMT_UNCOMPRESSED, FMT_FRAME_BASED, FMT_MJPEG) and ln >= 5:
                guid = bytes(d[5:21]) if st != FMT_MJPEG and ln >= 21 else b"MJPG"
                cur = FormatDesc(d[3], st, guid)
                formats[cur.index] = cur
            elif st in (FRM_UNCOMPRESSED, FRM_FRAME_BASED, FRM_MJPEG) and cur is not None and ln >= 26:
                w, h = struct.unpack_from("<HH", d, 5)
                if st == FRM_FRAME_BASED:
                    default = struct.unpack_from("<I", d, 17)[0]
                    itype = d[21]
                    max_size = struct.unpack_from("<I", d, 22)[0] * h
                else:
                    max_size = struct.unpack_from("<I", d, 17)[0]
                    default = struct.unpack_from("<I", d, 21)[0]
                    itype = d[25]
                ivals: List[int] = []
                cont = None
                if itype == 0:
                    if ln >= 38:
                        cont = struct.unpack_from("<III", d, 26)
                else:
                    for k in range(itype):
                        if 26 + 4 * k + 4 <= ln:
                            ivals.append(struct.unpack_from("<I", d, 26 + 4 * k)[0])
                cur.frames.append(FrameDesc(d[3], w, h, max_size, default, ivals, cont))
        i += ln
    return formats


def _join_extra(x) -> bytes:
    if not x:
        return b""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    return b"".join(bytes(e) for e in x)


def read_stream_interfaces(dev) -> List[StreamIface]:
    """usb1.USBDevice -> UVC VideoStreaming arayüzleri (cihaz AÇILMAZ)."""
    out: Dict[int, StreamIface] = {}
    control_iface = None
    for cfg in dev.iterConfigurations():
        for iface in cfg.iterInterfaces():
            for s in iface.iterSettings():
                if s.getClass() != 14:
                    continue
                if s.getSubClass() == 1 and control_iface is None:
                    control_iface = s.getNumber()
                if s.getSubClass() != 2:
                    continue
                si = out.setdefault(s.getNumber(), StreamIface(s.getNumber()))
                extra = _join_extra(s.getExtra())
                if extra and not si.formats:
                    si.formats = parse_vs_extra(extra)
                alt = AltSetting(s.getAlternateSetting())
                for e in s.iterEndpoints():
                    if e.getAddress() & 0x80:
                        mp = e.getMaxPacketSize()
                        alt.endpoint = e.getAddress()
                        alt.ep_type = e.getAttributes() & 3
                        alt.ep_packet = (mp & 0x7FF) * (1 + ((mp >> 11) & 3))
                si.alts.append(alt)
        break                                   # yalnızca ilk yapılandırma
    res = list(out.values())
    for si in res:
        si.control_iface = control_iface
    return res


# ==========================================================================
# Mod seçimi, probe/commit yapısı, alt-setting seçimi
# ==========================================================================
def pick_interval(fr: FrameDesc, want: int) -> int:
    if fr.intervals:
        return min(fr.intervals, key=lambda v: abs(v - want))
    if fr.continuous:
        mn, mx, _ = fr.continuous
        return max(mn, min(mx, want))
    return fr.default_interval


def choose_mode(si: StreamIface, width: int, height: int, fps: float):
    """(FormatDesc, FrameDesc, interval) ya da None. YUY2 tercih edilir, MJPEG elenir."""
    want = int(1e7 / fps)
    best = None
    for fmt in si.formats.values():
        if fmt.subtype == FMT_MJPEG:
            continue
        for fr in fmt.frames:
            if fr.width == width and fr.height == height:
                iv = pick_interval(fr, want)
                key = (0 if fmt.guid[:4] == b"YUY2" else 1, abs(iv - want))
                if best is None or key < best[0]:
                    best = (key, fmt, fr, iv)
    return None if best is None else best[1:]


def build_probe(base: bytes, fmt_index: int, frame_index: int, interval: int) -> bytes:
    b = bytearray(base if len(base) >= 26 else bytes(26))
    struct.pack_into("<H", b, 0, 1)          # bmHint: dwFrameInterval sabit
    b[2] = fmt_index
    b[3] = frame_index
    struct.pack_into("<I", b, 4, interval)
    return bytes(b)


def pick_alt(si: StreamIface, max_payload: int) -> Optional[AltSetting]:
    iso = sorted((a for a in si.alts if a.ep_type == EP_ISO and a.ep_packet),
                 key=lambda a: a.ep_packet)
    for a in iso:
        if a.ep_packet >= max_payload:
            return a
    if iso:
        return iso[-1]
    return next((a for a in si.alts if a.ep_type == EP_BULK), None)


# ==========================================================================
# UVC payload -> kare birleştirme
# ==========================================================================
class FrameAssembler:
    """UVC payload başlığı: [bHeaderLength, bmHeaderInfo, ...]
    bmHeaderInfo: bit0 FID, bit1 EOF, bit6 ERR."""

    def __init__(self, frame_size: int, on_frame: Callable[[bytes], None]):
        self.frame_size = frame_size
        self.on_frame = on_frame
        self.frames = 0
        self.dropped = 0
        self._buf = bytearray()
        self._fid: Optional[int] = None
        self._bad = False

    def feed(self, data: bytes) -> None:
        n = len(data)
        if n < 2:
            return
        hlen, info = data[0], data[1]
        if hlen < 2 or hlen > n:
            return
        fid = info & 1
        if self._fid is not None and fid != self._fid:
            self._finish()
        self._fid = fid
        if info & 0x40:
            self._bad = True
        if n > hlen:
            self._buf += data[hlen:]
            if len(self._buf) > self.frame_size * 2:      # kontrolsüz büyüme
                self._bad = True
                self._buf = bytearray()
        if info & 0x02:
            self._finish()

    def _finish(self) -> None:
        buf, bad = self._buf, self._bad
        self._buf, self._bad = bytearray(), False
        if not buf:
            return
        if bad or len(buf) != self.frame_size:
            self.dropped += 1
            return
        self.frames += 1
        self.on_frame(bytes(buf))


def decode_temp_words(frame: bytes) -> np.ndarray:
    """256x384 YUY2 -> (192,256) uint16 SICAKLIK kelimeleri (Kelvin x 64).

    DONANIMDA DOĞRULANDI (0bda:5830):
      * alt yarı (satır 192-383): her piksel 16-bit küçük-endian; kanal0 = düşük bayt,
        kanal1 = yüksek bayt.   sıcaklık(°C) = kelime/64 - 273.15
      * üst yarı (satır 0-191): kameranın kendi 8-bit görüntüsü (kanal1 hep 128)
    """
    arr = np.frombuffer(frame, dtype=np.uint8).reshape(STREAM_H, STREAM_W, 2)
    lo = arr[SENSOR_H:, :, 0].astype(np.uint16)
    hi = arr[SENSOR_H:, :, 1].astype(np.uint16)
    return (hi << 8) | lo


def decode_image8(frame: bytes) -> np.ndarray:
    """Üst yarı: kameranın kendi 8-bit (AGC'li) görüntüsü, (192,256) uint8."""
    arr = np.frombuffer(frame, dtype=np.uint8).reshape(STREAM_H, STREAM_W, 2)
    return arr[:SENSOR_H, :, 0].copy()


def words_to_celsius(words: np.ndarray) -> np.ndarray:
    return words.astype(np.float32) / 64.0 - 273.15


decode_y16_packed = decode_temp_words      # eski ad (uyumluluk)


# ==========================================================================
# Cihaz bulma
# ==========================================================================
def list_uvc_devices(width: int = STREAM_W, height: int = STREAM_H, only_openable: bool = True):
    """[(etiket, vid, pid)] - yalnızca width x height modu olan UVC cihazlar.

    only_openable=True: cihaz kısaca açılıp kapatılır; açılamayanlar (Windows'ta sürücü
    libusbK/WinUSB değilse usbvideo tutuyordur) listeye ALINMAZ. Böylece libusbK kurulu
    değilken ölü bir 'usb:' girişi görünmez ve uygulama ffmpeg yoluyla çalışmaya devam eder.
    Başka bir yol (UsbCapture) cihazı zaten açıkken False verin."""
    if usb1 is None:
        return []
    out = []
    try:
        with usb1.USBContext() as ctx:
            for dev in ctx.getDeviceIterator(skip_on_error=True):
                try:
                    for si in read_stream_interfaces(dev):
                        if not choose_mode(si, width, height, 25):
                            continue
                        if only_openable:
                            try:
                                dev.open().close()
                            except Exception:
                                break                      # açılamıyor -> listeye alma
                        out.append((f"UVC {width}x{height}", dev.getVendorID(), dev.getProductID()))
                        break
                except Exception:
                    continue
    except Exception:
        return []
    return out


# ==========================================================================
# Akış
# ==========================================================================
class UvcStream:
    NUM_TRANSFERS = 8
    ISO_PACKETS = 16

    def __init__(self, vid: Optional[int] = None, pid: Optional[int] = None,
                 width: int = STREAM_W, height: int = STREAM_H, fps: float = 25,
                 log: Callable[[str], None] = print):
        self.vid, self.pid = vid, pid
        self.width, self.height, self.fps = width, height, fps
        self.log = log
        self.ctx = None
        self.h = None
        self.si: Optional[StreamIface] = None
        self.alt: Optional[AltSetting] = None
        self.asm: Optional[FrameAssembler] = None
        self.failed: Optional[str] = None
        self._transfers: list = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._claimed: List[int] = []
        self._err_run = 0

    # -- açma -----------------------------------------------------------
    def open(self, on_frame: Callable[[bytes], None]) -> None:
        if usb1 is None:
            raise RuntimeError("libusb1 kurulu değil: pip install libusb1")
        self.ctx = usb1.USBContext()
        self.ctx.open()
        dev, si = self._find()
        self.si = si
        mode = choose_mode(si, self.width, self.height, self.fps)
        if mode is None:
            raise RuntimeError(f"{self.width}x{self.height} modu bulunamadı")
        fmt, fr, interval = mode
        self.log(f"mod: format#{fmt.index} frame#{fr.index} {fr.width}x{fr.height} "
                 f"{1e7 / interval:.1f} fps")

        try:
            self.h = dev.open()
        except usb1.USBError as e:
            raise RuntimeError(
                f"cihaz açılamadı ({e}). Sürücü libusbK/WinUSB değil olabilir (Zadig).") from e
        try:
            self.h.setAutoDetachKernelDriver(True)       # Linux; Windows'ta yok sayılır
        except Exception:
            pass
        for num in (si.control_iface, si.number):
            if num is None:
                continue
            try:
                self.h.claimInterface(num)
                self._claimed.append(num)
            except usb1.USBError as e:
                self.log(f"arayüz {num} alınamadı: {e}")
                if num == si.number:
                    raise RuntimeError("akış arayüzü alınamadı") from e

        max_payload = self._negotiate(fmt, fr, interval)
        self.alt = pick_alt(si, max_payload)
        if self.alt is None or self.alt.endpoint is None:
            raise RuntimeError("uygun endpoint/alt-setting bulunamadı")
        self.log(f"alt={self.alt.number} ep=0x{self.alt.endpoint:02X} "
                 f"tip={'iso' if self.alt.ep_type == EP_ISO else 'bulk'} "
                 f"paket={self.alt.ep_packet} payload={max_payload}")

        self.asm = FrameAssembler(self.width * self.height * 2, on_frame)
        if self.alt.ep_type == EP_ISO:
            self.h.setInterfaceAltSetting(si.number, self.alt.number)
        self._start_transfers(max_payload)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _find(self):
        for dev in self.ctx.getDeviceIterator(skip_on_error=True):
            if self.vid is not None and (dev.getVendorID(), dev.getProductID()) != (self.vid, self.pid):
                continue
            for si in read_stream_interfaces(dev):
                if choose_mode(si, self.width, self.height, self.fps):
                    return dev, si
        raise RuntimeError("uygun UVC cihaz bulunamadı")

    def _ctrl_in(self, req: int, sel: int, length: int) -> bytes:
        return bytes(self.h.controlRead(0xA1, req, sel << 8, self.si.number, length, 1000))

    def _ctrl_out(self, req: int, sel: int, data: bytes) -> None:
        self.h.controlWrite(0x21, req, sel << 8, self.si.number, bytes(data), 1000)

    def _negotiate(self, fmt: FormatDesc, fr: FrameDesc, interval: int) -> int:
        cur = self._ctrl_in(UVC_GET_CUR, VS_PROBE, 48)
        probe = build_probe(cur, fmt.index, fr.index, interval)
        self._ctrl_out(UVC_SET_CUR, VS_PROBE, probe)
        neg = bytearray(self._ctrl_in(UVC_GET_CUR, VS_PROBE, len(probe)))
        if len(neg) < 26:
            neg = bytearray(probe)
        if neg[2] != fmt.index or neg[3] != fr.index:
            self.log("uyarı: cihaz farklı format/frame döndürdü; istenen değerler commit edilecek")
            neg[2], neg[3] = fmt.index, fr.index
        self._ctrl_out(UVC_SET_CUR, VS_COMMIT, bytes(neg))
        payload = struct.unpack_from("<I", neg, 22)[0]
        return payload if 0 < payload <= 0x10000 else 1024

    # -- transferler ----------------------------------------------------
    def _start_transfers(self, max_payload: int) -> None:
        iso = self.alt.ep_type == EP_ISO
        for _ in range(self.NUM_TRANSFERS):
            if iso:
                t = self.h.getTransfer(iso_packets=self.ISO_PACKETS)
                t.setIsochronous(self.alt.endpoint, self.ISO_PACKETS * self.alt.ep_packet,
                                 callback=self._cb, timeout=1000)
            else:
                t = self.h.getTransfer()
                t.setBulk(self.alt.endpoint, max(max_payload, 512), callback=self._cb, timeout=1000)
            t.submit()
            self._transfers.append(t)

    def _cb(self, t) -> None:
        st = t.getStatus()
        if st in (usb1.TRANSFER_CANCELLED,):
            return
        if st == usb1.TRANSFER_NO_DEVICE:
            self.failed = "kamera çıkarıldı"
            return
        if self.alt.ep_type == EP_ISO:
            for pst, buf in t.iterISO():
                if pst == usb1.TRANSFER_COMPLETED and buf:
                    self.asm.feed(bytes(buf))
            self._err_run = 0
        elif st == usb1.TRANSFER_COMPLETED:
            self.asm.feed(bytes(t.getBuffer())[:t.getActualLength()])
            self._err_run = 0
        elif st != usb1.TRANSFER_TIMED_OUT:
            self._err_run += 1
            if self._err_run > 500:
                self.failed = f"akış hataları (durum {st})"
                return
        if not self._stop.is_set():
            try:
                t.submit()
            except usb1.USBError as e:
                self.failed = f"transfer yeniden gönderilemedi: {e}"

    def _loop(self) -> None:
        deadline = None
        while True:
            if self._stop.is_set():
                if deadline is None:
                    deadline = time.time() + 3.0
                if not any(t.isSubmitted() for t in self._transfers) or time.time() > deadline:
                    break
            try:
                self.ctx.handleEventsTimeout(0.1)
            except usb1.USBErrorInterrupted:
                continue
            except usb1.USBError as e:
                self.failed = f"libusb olay hatası: {e}"
                break

    # -- vendor komutları (aynı tutamaç) ---------------------------------
    def vendor_transfer(self, rt: int, req: int, val: int, idx: int, data):
        if rt & 0x80:
            return bytes(self.h.controlRead(rt, req, val, idx, int(data), 1000))
        return self.h.controlWrite(rt, req, val, idx, bytes(data), 1000)

    # -- kapatma --------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        for t in self._transfers:
            try:
                if t.isSubmitted():
                    t.cancel()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=4)
        if self.h is not None:
            try:
                if self.alt is not None and self.alt.ep_type == EP_ISO:
                    self.h.setInterfaceAltSetting(self.si.number, 0)
            except Exception:
                pass
            for num in self._claimed:
                try:
                    self.h.releaseInterface(num)
                except Exception:
                    pass
            try:
                self.h.close()
            except Exception:
                pass
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
        self.h = self.ctx = None


# ==========================================================================
# ThermalScope'taki Capture ile aynı arayüz
# ==========================================================================
class UsbCapture(threading.Thread):
    """snapshot(), fps, frame_count, stop(), is_alive() - Capture ile uyumlu."""

    def __init__(self, vid: Optional[int], pid: Optional[int], on_error,
                 width: int = STREAM_W, height: int = STREAM_H, fps: float = 25):
        super().__init__(daemon=True)
        self.vid, self.pid = vid, pid
        self.on_error = on_error
        self.width, self.height, self.fps_req = width, height, fps
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest: Optional[np.ndarray] = None          # Kelvin x 64, uint16
        self.latest_image: Optional[np.ndarray] = None    # kameranın 8-bit görüntüsü
        self.fps = 0.0
        self.frame_count = 0
        self.stream: Optional[UvcStream] = None
        self._t0 = time.time()
        self._n = 0

    def _on_frame(self, data: bytes) -> None:
        raw = decode_temp_words(data)
        with self.lock:
            self.latest = raw
            self.latest_image = decode_image8(data)
            self.frame_count += 1
        self._n += 1
        now = time.time()
        if now - self._t0 >= 1.0:
            self.fps = self._n / (now - self._t0)
            self._n, self._t0 = 0, now

    def run(self) -> None:
        self.stream = UvcStream(self.vid, self.pid, self.width, self.height, self.fps_req,
                                log=lambda m: None)
        try:
            self.stream.open(self._on_frame)
        except Exception as exc:
            self.on_error(f"USB akışı açılamadı: {exc}")
            self.stream.close()
            return
        while not self.stop_event.wait(0.5):
            if self.stream.failed:
                self.on_error(f"USB akışı durdu: {self.stream.failed}")
                break
        self.stream.close()

    def snapshot(self) -> Optional[np.ndarray]:
        with self.lock:
            return None if self.latest is None else self.latest.copy()

    def snapshot_image(self) -> Optional[np.ndarray]:
        with self.lock:
            return None if self.latest_image is None else self.latest_image.copy()

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5)

    @property
    def vendor(self):
        """Kalibrasyon vendor komutları için UtiDevice (uti256_usb.py)."""
        from uti256_usb import UtiDevice
        return UtiDevice(self.stream.vendor_transfer)


# ==========================================================================
# CLI
# ==========================================================================
def _parse_id(s: str) -> Tuple[int, int]:
    v, p = s.split(":")
    return int(v, 16), int(p, 16)


def cmd_list() -> None:
    if usb1 is None:
        print("libusb1 yok"); return
    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            try:
                ifs = read_stream_interfaces(dev)
            except Exception:
                ifs = []
            tag = ""
            if ifs:
                modes = {(f.width, f.height) for si in ifs for fm in si.formats.values() for f in fm.frames}
                tag = "  UVC modlar: " + ", ".join(f"{w}x{h}" for w, h in sorted(modes))
            print(f"{dev.getVendorID():04x}:{dev.getProductID():04x}{tag}")


def cmd_probe(vid: int, pid: int) -> None:
    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            if (dev.getVendorID(), dev.getProductID()) != (vid, pid):
                continue
            for si in read_stream_interfaces(dev):
                print(f"VS arayüzü {si.number} (kontrol arayüzü {si.control_iface})")
                for fm in si.formats.values():
                    print(f"  format#{fm.index} alttip=0x{fm.subtype:02X} guid={fm.guid.hex()}")
                    for fr in fm.frames:
                        ivs = [f"{1e7 / v:.1f}" for v in fr.intervals] or [str(fr.continuous)]
                        print(f"    frame#{fr.index} {fr.width}x{fr.height} fps={ivs} max={fr.max_frame_size}")
                for a in si.alts:
                    tip = {1: "iso", 2: "bulk", 3: "int", None: "-"}[a.ep_type]
                    ep = f"0x{a.endpoint:02X}" if a.endpoint is not None else "-"
                    print(f"  alt {a.number}: ep={ep} {tip} paket={a.ep_packet}")


def cmd_vendor_test(vid: int, pid: int) -> None:
    st = UvcStream(vid, pid, log=print)
    got = []
    st.open(got.append)
    try:
        for rt in (0xC1, 0xC0, 0xC2):
            try:
                b = st.vendor_transfer(rt, 0x44, 0x78, 0x0200, 1)
                print(f"durum okuma bmRequestType=0x{rt:02X}: {b.hex()}  <- çalıştı")
            except Exception as e:
                print(f"durum okuma bmRequestType=0x{rt:02X}: HATA {e}")
        from uti256_usb import UtiDevice
        dev = UtiDevice(st.vendor_transfer)
        try:
            raw = dev.spi_read(0xD1000, 32)
            print("SPI 0xD1000 ilk 32 bayt:", raw.hex())
        except Exception as e:
            print("SPI okuma hatası:", e)
    finally:
        st.close()


def cmd_grab(vid: int, pid: int, n: int) -> None:
    frames = []
    st = UvcStream(vid, pid, log=print)
    st.open(frames.append)
    t0 = time.time()
    while len(frames) < n and time.time() - t0 < 15 and not st.failed:
        time.sleep(0.1)
    dt = time.time() - t0
    asm = st.asm
    st.close()
    print(f"{len(frames)} kare, {dt:.1f} sn, ~{len(frames) / max(dt, 1e-6):.1f} fps, "
          f"atılan={asm.dropped if asm else '?'} hata={st.failed}")
    if frames:
        raw = decode_temp_words(frames[-1])
        np.save("grab_last.npy", raw)
        t = words_to_celsius(raw)
        print(f"son kare grab_last.npy (sıcaklık kelimeleri); °C: min={t.min():.1f} medyan={np.median(t):.1f} maks={t.max():.1f}")


def cmd_grabraw(vid: int, pid: int, n: int) -> None:
    """Tam ham kareyi (384x256x2 bayt, İKİ kanal da) kaydeder: grab_raw.npy / grab_raw.bin"""
    frames = []
    st = UvcStream(vid, pid, log=print)
    st.open(frames.append)
    t0 = time.time()
    while len(frames) < n and time.time() - t0 < 15 and not st.failed:
        time.sleep(0.1)
    st.close()
    if not frames:
        print("kare alınamadı:", st.failed)
        return
    last = frames[-1]
    with open("grab_raw.bin", "wb") as fh:
        fh.write(last)
    arr = np.frombuffer(last, dtype=np.uint8).reshape(STREAM_H, STREAM_W, 2)
    np.save("grab_raw.npy", arr)
    print(f"{len(frames)} kare alındı; son kare grab_raw.npy ({arr.shape}) ve grab_raw.bin olarak kaydedildi")


# ==========================================================================
# Donanımsız doğrulama
# ==========================================================================
def _desc_format(idx, nframes):
    guid = b"YUY2" + bytes.fromhex("00001000800000aa00389b71")
    return bytes([27, 0x24, 0x04, idx, nframes]) + guid + bytes([16, 1, 0, 0, 0, 0])


def _desc_frame(idx, w, h, intervals):
    b = bytearray([26 + 4 * len(intervals), 0x24, 0x05, idx, 0])
    b += struct.pack("<HH", w, h)
    b += struct.pack("<II", w * h * 16 * 25, w * h * 16 * 25)
    b += struct.pack("<II", w * h * 2, intervals[0])
    b += bytes([len(intervals)])
    for v in intervals:
        b += struct.pack("<I", v)
    return bytes(b)


def _selftest() -> None:
    import random

    # 1) tanımlayıcı ayrıştırma + mod seçimi
    extra = (bytes([13, 0x24, 0x01, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0])
             + _desc_format(1, 2)
             + _desc_frame(1, 256, 192, [400000])
             + _desc_frame(2, 256, 384, [400000, 666666, 1000000])
             + bytes([6, 0x24, 0x03, 0, 0, 0]))
    fm = parse_vs_extra(extra)
    assert list(fm) == [1] and len(fm[1].frames) == 2
    si = StreamIface(1, fm, [AltSetting(0), AltSetting(1, 0x81, EP_ISO, 128),
                             AltSetting(2, 0x81, EP_ISO, 512), AltSetting(3, 0x81, EP_ISO, 1024)])
    f, fr, iv = choose_mode(si, 256, 384, 25)
    assert (f.index, fr.index, iv) == (1, 2, 400000)
    assert choose_mode(si, 640, 480, 25) is None
    assert choose_mode(si, 256, 384, 15)[2] == 666666

    # 2) probe yapısı
    base = bytes(26)
    p = build_probe(base, 1, 2, 400000)
    assert struct.unpack_from("<HBBI", p, 0) == (1, 1, 2, 400000)

    # 3) alt-setting seçimi
    assert pick_alt(si, 400).number == 2
    assert pick_alt(si, 5000).number == 3
    assert pick_alt(StreamIface(1, fm, [AltSetting(0, 0x82, EP_BULK, 512)]), 512).ep_type == EP_BULK

    # 4) kare birleştirme: iyi, hatalı, kısa, iyi
    size = 256 * 384 * 2
    got = []
    asm = FrameAssembler(size, got.append)
    random.seed(3)

    def send(frame: bytes, fid: int, err=False, chunk=1000, eof=True):
        pos = 0
        while pos < len(frame):
            part = frame[pos:pos + chunk]
            pos += chunk
            info = fid | (0x02 if (pos >= len(frame) and eof) else 0) | (0x40 if err and pos == chunk else 0)
            asm.feed(bytes([12, info]) + bytes(10) + part)

    f1 = bytes(random.getrandbits(8) for _ in range(size))
    f2 = bytes(random.getrandbits(8) for _ in range(size))
    f3 = bytes(random.getrandbits(8) for _ in range(size))
    send(f1, 0)                     # geçerli
    send(f2, 1, err=True)           # ERR bayrağı -> atılmalı
    send(f3[:50000], 0)             # kısa -> atılmalı
    send(f3, 1, eof=False)          # EOF yok; FID değişince bitmeli
    send(f1, 0)                     # FID değişimi f3'ü bitirir; sonra f1 kendi EOF'uyla biter
    assert got == [f1, f3, f1], (len(got),)
    assert asm.dropped == 2 and asm.frames == 3

    raw = decode_temp_words(f1)
    assert raw.shape == (192, 256) and raw.dtype == np.uint16
    # bilinen değer: 20.0 °C = (20+273.15)*64 = 18761.6 -> 18760 (4'ün katı), küçük-endian
    w = 18760
    fr = bytearray(size)
    for y in range(192, 384):
        for x in range(256):
            fr[(y * 256 + x) * 2] = w & 255
            fr[(y * 256 + x) * 2 + 1] = w >> 8
    t = words_to_celsius(decode_temp_words(bytes(fr)))
    assert abs(float(t[0, 0]) - 19.99) < 0.05, t[0, 0]
    print("descriptor, mod/alt seçimi, probe ve kare birleştirme: OK")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "--selftest":
        _selftest()
    elif a[0] == "--list":
        cmd_list()
    elif a[0] == "--probe":
        cmd_probe(*_parse_id(a[1]))
    elif a[0] == "--vendor-test":
        cmd_vendor_test(*_parse_id(a[1]))
    elif a[0] == "--grabraw":
        cmd_grabraw(*_parse_id(a[1]), int(a[2]) if len(a) > 2 else 20)
    elif a[0] == "--grab":
        cmd_grab(*_parse_id(a[1]), int(a[2]) if len(a) > 2 else 50)
    else:
        print(__doc__)
