"""Self-contained SMS sender for the TRM240 (Quectel EC21) cellular modem.

This is the *secondary* SMS path — the true last resort that needs no IP at
all, only the radio. It talks AT commands directly over the modem's serial AT
port (default ``/dev/ttyUSB2``) using nothing but the Python standard library,
so there is no system package (gammu / ModemManager) to install or keep in sync.

Messages are sent as proper SMS-SUBMIT PDUs (``AT+CMGF=0``):

  * GSM 03.38 7-bit packing when every character is representable in the
    default alphabet (covers Norwegian å/æ/ø and the GSM extension set), giving
    160 characters per part.
  * UCS2 (UTF-16BE) for anything else (e.g. a literal ``—`` em-dash or emoji),
    giving 70 characters per part.
  * Long messages are split into concatenated parts with an 8-bit-reference
    UDH so the handset reassembles them into one message.

The encoder is the fiddly bit, so it is unit-tested against known-good PDUs in
``tests/test_modem_sms.py``. Keep those passing if you touch this file.
"""
from __future__ import annotations

import argparse
import os
import random
import select
import sys
import termios
import time
import tty

# --- GSM 03.38 default alphabet ----------------------------------------------
# Index == 7-bit value; the character at that position. ESC (0x1B) marks the
# two-septet extension sequences handled separately below.
_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
assert len(_GSM_BASIC) == 128

# Characters reached via the ESC (0x1B) prefix — each costs two septets.
_GSM_EXT = {
    "\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F,
    "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "€": 0x65,
}

_BASIC_INDEX = {c: i for i, c in enumerate(_GSM_BASIC)}

# Per-part capacity (septets for GSM-7, UTF-16 code units for UCS2).
_GSM7_SINGLE, _GSM7_MULTI = 160, 153
_UCS2_SINGLE, _UCS2_MULTI = 70, 67


class ModemError(Exception):
    """Raised when the modem rejects a command or never answers."""


# --- Encoding ----------------------------------------------------------------

def _to_septets(text: str) -> list[int] | None:
    """Return the GSM-7 septet sequence for *text*, or None if it needs UCS2."""
    out: list[int] = []
    for ch in text:
        if ch in _BASIC_INDEX:
            out.append(_BASIC_INDEX[ch])
        elif ch in _GSM_EXT:
            out.extend((0x1B, _GSM_EXT[ch]))
        else:
            return None
    return out


def _pack7(septets: list[int], fill_bits: int = 0) -> bytes:
    """Pack 7-bit values into octets, LSB-first, with optional leading fill bits
    (used to byte-align the payload after an octet-sized UDH)."""
    acc = 0
    nbits = fill_bits
    out = bytearray()
    for s in septets:
        acc |= (s & 0x7F) << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def _encode_address(number: str) -> str:
    """Encode the destination address field (len + type + swapped digits)."""
    intl = number.startswith("+")
    digits = number[1:] if intl else number
    digits = "".join(c for c in digits if c.isdigit())
    toa = 0x91 if intl else 0x81
    padded = digits + "F" if len(digits) % 2 else digits
    swapped = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    return f"{len(digits):02X}{toa:02X}{swapped}"


def _udh(ref: int, total: int, seq: int) -> bytes:
    """Concatenated-message header, 8-bit reference (IEI 0x00)."""
    return bytes((0x05, 0x00, 0x03, ref & 0xFF, total, seq))


def _split_septets(septets: list[int], size: int) -> list[list[int]]:
    parts, i = [], 0
    while i < len(septets):
        end = min(i + size, len(septets))
        # Never split an ESC (0x1B) away from its following extension code.
        if end < len(septets) and septets[end - 1] == 0x1B:
            end -= 1
        parts.append(septets[i:end])
        i = end
    return parts


def _split_units(units: list[int], size: int) -> list[list[int]]:
    parts, i = [], 0
    while i < len(units):
        end = min(i + size, len(units))
        # Don't split a UTF-16 surrogate pair across parts.
        if end < len(units) and 0xD800 <= units[end - 1] <= 0xDBFF:
            end -= 1
        parts.append(units[i:end])
        i = end
    return parts


def build_pdus(number: str, text: str, ref: int | None = None) -> list[str]:
    """Build the SMS-SUBMIT PDU hex string(s) for one message (without the
    AT+CMGS length prefix). One string per concatenation part."""
    da = _encode_address(number)
    septets = _to_septets(text)

    if septets is not None:  # GSM-7
        dcs = 0x00
        if len(septets) <= _GSM7_SINGLE:
            segments, multipart = [septets], False
        else:
            segments, multipart = _split_septets(septets, _GSM7_MULTI), True
    else:  # UCS2
        dcs = 0x08
        units = [b for ch in text for b in
                 (int.from_bytes(ch.encode("utf-16-be")[i:i + 2], "big")
                  for i in range(0, len(ch.encode("utf-16-be")), 2))]
        if len(units) <= _UCS2_SINGLE:
            segments, multipart = [units], False
        else:
            segments, multipart = _split_units(units, _UCS2_MULTI), True

    if ref is None:
        ref = random.randint(0, 255)
    total = len(segments)
    pdus: list[str] = []

    for idx, seg in enumerate(segments, start=1):
        first = 0x01 | (0x40 if multipart else 0x00)  # SMS-SUBMIT, UDHI if concat
        udh = _udh(ref, total, idx) if multipart else b""

        if dcs == 0x00:  # GSM-7
            if udh:
                udh_septets = (len(udh) * 8 + 6) // 7
                fill = udh_septets * 7 - len(udh) * 8
                ud = udh + _pack7(seg, fill)
                udl = udh_septets + len(seg)
            else:
                ud = _pack7(seg)
                udl = len(seg)
        else:  # UCS2
            payload = b"".join(u.to_bytes(2, "big") for u in seg)
            ud = udh + payload
            udl = len(ud)

        pdu = (f"00{first:02X}00{da}00{dcs:02X}{udl:02X}"
               + ud.hex().upper())
        pdus.append(pdu)
    return pdus


# --- Serial / AT transport ---------------------------------------------------

def _open_port(port: str, baud: int = termios.B115200) -> int:
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[4] = attrs[5] = baud
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def _drain(fd: int) -> None:
    while select.select([fd], [], [], 0)[0]:
        try:
            if not os.read(fd, 4096):
                break
        except OSError:
            break


def _expect(fd: int, terminators: tuple[str, ...], timeout: float) -> str:
    """Read until one of *terminators* appears in the accumulated text."""
    end = time.time() + timeout
    buf = ""
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                continue
            if chunk:
                buf += chunk.decode(errors="replace")
                if any(t in buf for t in terminators):
                    return buf
    raise ModemError(f"timeout waiting for {terminators!r}; got {buf!r}")


def _command(fd: int, cmd: str, timeout: float = 5.0) -> str:
    os.write(fd, (cmd + "\r").encode())
    resp = _expect(fd, ("OK", "ERROR"), timeout)
    if "ERROR" in resp:
        raise ModemError(f"{cmd!r} -> {resp.strip()!r}")
    return resp


def send_sms(number: str, text: str, port: str | None = None,
             timeout: float = 30.0) -> str:
    """Send *text* to *number* over the modem. Returns a short status string;
    raises ModemError on failure."""
    port = port or os.environ.get("FW_MODEM_PORT", "/dev/ttyUSB2")
    pdus = build_pdus(number, text)
    fd = _open_port(port)
    try:
        _drain(fd)
        _command(fd, "ATE0")          # echo off — simpler parsing
        _command(fd, "AT+CMGF=0")     # PDU mode
        refs: list[str] = []
        for pdu in pdus:
            tpdu_len = len(pdu) // 2 - 1   # octets, excluding the SMSC byte
            os.write(fd, f"AT+CMGS={tpdu_len}\r".encode())
            _expect(fd, (">",), timeout=5.0)
            os.write(fd, (pdu + "\x1a").encode())  # PDU + Ctrl-Z
            resp = _expect(fd, ("+CMGS:", "ERROR"), timeout=timeout)
            if "ERROR" in resp:
                raise ModemError(f"send rejected: {resp.strip()!r}")
            for line in resp.splitlines():
                if "+CMGS:" in line:
                    refs.append(line.split(":", 1)[1].strip())
        return f"sent {len(pdus)} part(s), refs={','.join(refs)}"
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send an SMS via the TRM240 modem.")
    ap.add_argument("number", help="destination MSISDN, e.g. +4791234567")
    ap.add_argument("text", help="message body")
    ap.add_argument("--port", default=None, help="serial AT port (default /dev/ttyUSB2)")
    ap.add_argument("--dry-run", action="store_true", help="print PDUs, don't send")
    args = ap.parse_args(argv)
    if args.dry_run:
        for p in build_pdus(args.number, args.text):
            print(p)
        return 0
    try:
        print(send_sms(args.number, args.text, port=args.port))
        return 0
    except (ModemError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
