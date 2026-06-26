"""Unit tests for the TRM240 AT/PDU SMS encoder (app.modem_sms).

These exercise only the pure encoding — no serial port is opened.
"""
from app import modem_sms as m

NUM = "+4791234567"          # -> DA field "0A917419325476"
DA = "0A917419325476"


def test_address_even_international():
    assert m._encode_address(NUM) == DA


def test_address_odd_padded_with_f():
    # 3 digits -> pad to "123F", swap nibbles -> "21F3"; len counts real digits.
    assert m._encode_address("+123") == "039121F3"


def test_address_national_when_no_plus():
    assert m._encode_address("12345").startswith("0581")


def test_gsm7_hello_canonical_pdu():
    # "hello" packs to the well-known E8329BFD06; full SMS-SUBMIT below.
    pdus = m.build_pdus(NUM, "hello")
    assert pdus == ["0001000A917419325476000005E8329BFD06"]


def test_gsm7_packing_matches_known_vector():
    assert m._pack7([0x68, 0x65, 0x6C, 0x6C, 0x6F]).hex().upper() == "E8329BFD06"


def test_extension_char_costs_two_septets():
    assert m._to_septets("€") == [0x1B, 0x65]
    assert m._to_septets("{") == [0x1B, 0x28]


def test_norwegian_letters_stay_gsm7():
    # å æ ø are in the GSM default alphabet -> no UCS2, single part.
    pdus = m.build_pdus(NUM, "blåbær køntår")
    assert len(pdus) == 1
    assert pdus[0][22:24] == "00"  # DCS == 0x00 (GSM-7)


def test_em_dash_forces_ucs2():
    # "—" (U+2014) is not in GSM-7 -> UCS2, DCS 0x08, payload "2014".
    pdus = m.build_pdus(NUM, "—")
    assert pdus == ["0001000A9174193254760008022014"]


def test_gsm7_160_chars_single_part():
    assert len(m.build_pdus(NUM, "A" * 160)) == 1


def test_gsm7_161_chars_splits_with_udh():
    pdus = m.build_pdus(NUM, "A" * 161, ref=0x42)
    assert len(pdus) == 2
    # First octet has UDHI bit set (0x41); UDH (6 octets) carries ref 0x42,
    # total 2, sequence 1 then 2.
    assert pdus[0][2:4] == "41" and pdus[0][26:38] == "050003420201"
    assert pdus[1][2:4] == "41" and pdus[1][26:38] == "050003420202"
    # part1 carries 153 payload septets -> UDL 160 (0xA0); part2 -> 8 -> UDL 15.
    assert pdus[0][24:26] == "A0"
    assert pdus[1][24:26] == "0F"


def test_ucs2_71_chars_splits_with_udh():
    pdus = m.build_pdus(NUM, "—" * 71, ref=0x07)
    assert len(pdus) == 2
    for i in (0, 1):
        assert pdus[i][2:4] == "41"        # UDHI set
        assert pdus[i][22:24] == "08"      # DCS UCS2
        assert pdus[i][26:32] == "050003"  # concat UDH IEI


def test_unknown_placeholder_text_roundtrips_length():
    # A realistic alarm template body stays a single GSM-7 part.
    body = "FIRE ALARM: Kitchen (heat) in Zone 2. Temp 68C. Investigate immediately."
    pdus = m.build_pdus(NUM, body)
    assert len(pdus) == 1
    assert pdus[0][22:24] == "00"
