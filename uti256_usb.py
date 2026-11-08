#!/usr/bin/env python3
"""
uti256_usb.py - UNI-T Thermal Mobile (256x384 / IRCMD) vendor USB protokolü.

libircmd.so ve libUSBUVCCamera.so disassemble edilerek çıkarılmıştır
(UVCCamera::sendCommand == libusb_control_transfer). Salt-okunur komutlar
(durum, SPI okuma) donanımda doğrulandı (VID:PID 0bda:5830, libusbK).
Yazma/ayar komutları donanımda HENÜZ doğrulanmadı.

Bu bir UVC "extension unit" (class 0x21/0xA1) DEĞİLDİR; vendor control request'tir.

Taşıma (hepsi wValue = 0x78, timeout 1000 ms):
  YAZ : bmRequestType 0x41, bRequest 0x45      OKU : 0xC1, 0x44
  wIndex:
     0x1D00  8 baytlık komut başlığı, veri aşaması yok
     0x9D00  8 baytlık komut başlığı, ardından veri aşaması VAR
     0x1D08  veri tamponu (yazma: komut verisi / okuma: yanıt)
     0x0200  durum baytı (1 bayt): bit0 = meşgul; (durum & 3) == 0 -> tamam

Komut başlıkları (8 bayt, kod little-endian):
  ooc_b_update(tip)       0x1D00  [0D C1 tip 00 00 00 00 00]
  recal 1 nokta           0x9D00  [0E C4 00 00 00 00 00 02] + veri: Kelvin (BE16)
  recal 2-nokta BİRİNCİ   0x9D00  [0E C5 00 00 00 00 00 02] + veri: Kelvin (BE16)
  recal 2-nokta SON       0x9D00  [0E C5 01 00 00 00 00 02] + veri: Kelvin (BE16)
  set_prop_tpd_params     0x9D00  [14 C5 00 id 00 00 vH vL] + 8 sıfır bayt veri
  set_prop_auto_shutter   0x9D00  [14 C2 00 id 00 00 vH vL] + 8 sıfır bayt veri
  spi_read                0x1D00  [01 82 addr(4,BE) len(2,BE)] -> 0x1D08'den oku (<=64)

Enum değerleri (Java'dan):  UpdateOOCOrB: B_UPDATE=0, OOC_UPDATE=1, OOC_B_UPDATE=2
  TPDKtBtRecalPoint: 1_POINT=0, 2_POINT_FIRST=1, 2_POINT_END=2
  TPD_PROP_GAIN_SEL=5 (LOW=0, HIGH=1)   SHUTTER_PROP_SWITCH=0 (OFF=0, ON=1)
  Kelvin = °C + 273 (tam sayı)

GÜVENLİK: Bu dosya bilerek spi_write / spi_erase / spi_config_save İÇERMEZ;
kalıcı flash yazımı yapmaz.
"""
from __future__ import annotations

import struct
import time
from typing import Callable, List

REQ_OUT, REQ_IN = 0x41, 0xC1
BREQ_WRITE, BREQ_READ = 0x45, 0x44
WVALUE = 0x78
WIDX_CMD, WIDX_CMD_DATA, WIDX_DATA, WIDX_STATUS = 0x1D00, 0x9D00, 0x1D08, 0x0200

KT_ADDR_GAIN0 = 0xD1000
KT_ADDR_OTHER = 0xD8000
KT_LEN = 0x962

CMD_OOC_B_UPDATE = 0xC10D
CMD_RECAL_1PT = 0xC40E
CMD_RECAL_2PT = 0xC50E
CMD_SET_TPD_PARAM = 0xC514
CMD_SET_AUTO_SHUTTER = 0xC214

B_UPDATE, OOC_UPDATE, OOC_B_UPDATE = 0, 1, 2
RECAL_1_POINT, RECAL_2_POINT_FIRST, RECAL_2_POINT_END = 0, 1, 2
TPD_PROP_GAIN_SEL = 5
SHUTTER_PROP_SWITCH = 0


class ProtocolError(RuntimeError):
    pass


class UtiDevice:
    """transfer(bmRequestType, bRequest, wValue, wIndex, data_or_len) -> bytes|int"""

    def __init__(self, transfer: Callable):
        self._xfer = transfer

    # -- düşük seviye ----------------------------------------------------
    def _write(self, windex: int, payload: bytes) -> None:
        self._xfer(REQ_OUT, BREQ_WRITE, WVALUE, windex, payload)

    def _read(self, windex: int, n: int) -> bytes:
        return bytes(self._xfer(REQ_IN, BREQ_READ, WVALUE, windex, n))

    def wait_ready(self, tries: int = 1000, delay: float = 0.001) -> None:
        for _ in range(tries):
            st = self._read(WIDX_STATUS, 1)[0]
            if st & 1:
                time.sleep(delay)
                continue
            if not (st & 2):
                return
            if st > 3:
                raise ProtocolError(f"cihaz hata durumu: 0x{st:02X}")
            time.sleep(delay)
        raise ProtocolError("durum yoklaması zaman aşımı")

    # -- komut kalıpları -------------------------------------------------
    @staticmethod
    def header(code: int, param: int = 0, data_len: int = 0) -> bytes:
        return struct.pack("<HB4xB", code & 0xFFFF, param & 0xFF, data_len & 0xFF)

    def command(self, code: int, param: int = 0) -> None:
        """Veri aşaması olmayan komut (ooc_b_update gibi)."""
        self._write(WIDX_CMD, self.header(code, param))
        self.wait_ready()

    def command_with_data(self, code: int, data: bytes, param: int = 0) -> None:
        """Başlığın son baytı veri uzunluğudur (recal komutları)."""
        self._write(WIDX_CMD_DATA, self.header(code, param, len(data)))
        self._write(WIDX_DATA, data)
        self.wait_ready()

    def command_prop(self, code: int, prop_id: int, value: int) -> None:
        """set_prop_* kalıbı: [kod, 00, id, 00, 00, değer(BE16)] + 8 sıfır bayt."""
        hdr = bytes([code & 0xFF, (code >> 8) & 0xFF, 0, prop_id & 0xFF, 0, 0,
                     (value >> 8) & 0xFF, value & 0xFF])
        self._write(WIDX_CMD_DATA, hdr)
        self._write(WIDX_DATA, bytes(8))
        self.wait_ready()

    # -- okuma (donanımda doğrulandı) ------------------------------------
    def spi_read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        while length > 0:
            n = min(length, 64)
            self._write(WIDX_CMD, bytes([0x01, 0x82]) + struct.pack(">IH", addr, n))
            self.wait_ready()
            out += self._read(WIDX_DATA, n)
            addr += n
            length -= n
        return bytes(out)

    def read_kt_raw(self, gain0: bool = True) -> List[int]:
        raw = self.spi_read(KT_ADDR_GAIN0 if gain0 else KT_ADDR_OTHER, KT_LEN)
        return list(struct.unpack(">%dH" % (len(raw) // 2), raw))

    # -- ayar komutları (donanımda doğrulanmadı) --------------------------
    def ooc_b_update(self, kind: int = B_UPDATE) -> None:
        self.command(CMD_OOC_B_UPDATE, kind)

    def recal_point(self, point: int, celsius: int) -> None:
        """point: RECAL_1_POINT / RECAL_2_POINT_FIRST / RECAL_2_POINT_END."""
        kelvin = int(celsius) + 273
        if not 0 < kelvin < 0x10000:
            raise ValueError("sıcaklık aralık dışı")
        code, param = {RECAL_1_POINT: (CMD_RECAL_1PT, 0),
                       RECAL_2_POINT_FIRST: (CMD_RECAL_2PT, 0),
                       RECAL_2_POINT_END: (CMD_RECAL_2PT, 1)}[point]
        self.command_with_data(code, struct.pack(">H", kelvin), param)

    def set_gain(self, high: bool) -> None:
        self.command_prop(CMD_SET_TPD_PARAM, TPD_PROP_GAIN_SEL, 1 if high else 0)

    def set_auto_shutter(self, on: bool) -> None:
        self.command_prop(CMD_SET_AUTO_SHUTTER, SHUTTER_PROP_SWITCH, 1 if on else 0)


def open_pyusb(vid: int, pid: int) -> UtiDevice:
    import usb.core
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise ProtocolError("cihaz bulunamadı")
    return UtiDevice(lambda rt, r, v, i, d: dev.ctrl_transfer(rt, r, v, i, d, 1000))


# --------------------------------------------------------------------------
def _selftest() -> None:
    log = []
    flash = bytes((i * 7) & 0xFF for i in range(0x1000))

    def fake(rt, r, v, i, d):
        log.append((rt, r, v, i, bytes(d) if isinstance(d, (bytes, bytearray)) else d))
        if rt == REQ_IN and i == WIDX_STATUS:
            return bytes([0])
        if rt == REQ_IN and i == WIDX_DATA:
            addr, n = struct.unpack(">IH", fake.hdr[2:8])
            return flash[addr:addr + n]
        if rt == REQ_OUT and i == WIDX_CMD:
            fake.hdr = bytes(d)
        return len(d) if isinstance(d, (bytes, bytearray)) else d
    fake.hdr = b""
    dev = UtiDevice(fake)

    def writes():
        return [(i, d) for rt, r, v, i, d in log if rt == REQ_OUT]

    dev.ooc_b_update(B_UPDATE)
    assert writes() == [(0x1D00, bytes.fromhex("0dc1000000000000"))]
    log.clear()
    dev.recal_point(RECAL_2_POINT_FIRST, 10)
    assert writes() == [(0x9D00, bytes.fromhex("0ec5000000000002")), (0x1D08, bytes.fromhex("011b"))]
    log.clear()
    dev.recal_point(RECAL_2_POINT_END, 120)
    assert writes() == [(0x9D00, bytes.fromhex("0ec5010000000002")), (0x1D08, bytes.fromhex("0189"))]
    log.clear()
    dev.recal_point(RECAL_1_POINT, 25)
    assert writes()[0] == (0x9D00, bytes.fromhex("0ec4000000000002"))
    log.clear()
    dev.set_gain(True)
    assert writes() == [(0x9D00, bytes.fromhex("14c5000500000001")), (0x1D08, bytes(8))]
    log.clear()
    dev.set_auto_shutter(False)
    assert writes() == [(0x9D00, bytes.fromhex("14c2000000000000")), (0x1D08, bytes(8))]
    log.clear()
    dev.set_auto_shutter(True)
    assert writes()[0] == (0x9D00, bytes.fromhex("14c2000000000001"))
    log.clear()
    assert dev.spi_read(0x100, 150) == flash[0x100:0x100 + 150]       # 64+64+22
    print("paket kurulumu (başlıklar libircmd ile eşleşiyor) ve SPI okuma: OK")


if __name__ == "__main__":
    _selftest()
