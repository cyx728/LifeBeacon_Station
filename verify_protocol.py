"""TX golden-vector, standalone log decoding and per-field UI/export verification."""
import argparse
import binascii
from collections import Counter
import csv
import json
from pathlib import Path
import tempfile

import receiver as rx


GOLDEN = bytes.fromhex('4c 42 01 01 00 87 cf d8 59 09 44 45 4d 4f 32 5f 50 43 42 '
                       '00 ff ff 00 00 00 bd 80 00 16 bc')
EXPECTED = dict(magic='LB', version=1, message_type=1, emergency=0,
                device_id=0x59D8CF87, name_length=9, name='DEMO2_PCB', physiology_flags=0,
                hr=None, spo2=None, hr_raw=255, spo2_raw=255, physiology_reserved=0,
                location_flag=0, battery=0, sequence=189, rssi_placeholder=128,
                reserved=0, crc16_ccitt_le=0xBC16)


def check_protocol():
    assert len(GOLDEN) == 30 and binascii.crc_hqx(GOLDEN[:-2], 0xffff) == 0xBC16
    assert rx.parse_lb(GOLDEN) == EXPECTED
    details = rx.lb_field_details(GOLDEN, EXPECTED)
    assert len(details) == 17
    assert details[4] == dict(field='device_id_le', key='device_id', offset_start=5,
                              offset_end=8, raw_hex='87 CF D8 59', value=0x59D8CF87)
    assert details[6]['offset_start'] == 10 and details[6]['offset_end'] == 18
    assert details[-1]['offset_start'] == 28 and details[-1]['raw_hex'] == '16 BC'
    assert bytes.fromhex(' '.join(segment['raw_hex'] for segment in details)) == GOLDEN
    # All user-defined message types share this LB v1 field layout.
    for message_type, label in [(0, 'CQ'), (1, 'beacon'), (2, 'response')]:
        body = GOLDEN[:3] + bytes([message_type]) + GOLDEN[4:-2]
        raw = body + binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')
        record = rx.decode_record(dict(raw_payload_hex=raw.hex(' '), phy_crc_valid=True))
        assert record['fields']['message_type'] == message_type
        assert record['message_type_label'] == label and record['status'] == 'PHY CRC OK / LB OK'
        corrupt = bytearray(raw)
        corrupt[4] ^= 1
        assert rx.decode_record(dict(raw_payload_hex=corrupt.hex(' '), phy_crc_valid=True))['fields'] is None
    unsupported = GOLDEN[:3] + b'\xff' + GOLDEN[4:-2]
    unsupported += binascii.crc_hqx(unsupported, 0xffff).to_bytes(2, 'little')
    assert rx.decode_record(dict(raw_payload_hex=unsupported.hex(' '), phy_crc_valid=True))['fields'] is None
    # A flag is required even when the measurement byte happens to look valid.
    for size, flags, hr, spo2 in [(0, 0, 72, 98), (31, 3, 72, 98), (1, 3, 255, 255)]:
        body = GOLDEN[:9] + bytes([size]) + b'A'*size + bytes([flags, hr, spo2, 0, 0, 50, 12, 128, 0])
        raw = body + binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')
        fields = rx.parse_lb(raw)
        assert fields['name_length'] == size and fields['sequence'] == 12
        assert fields['hr'] == (72 if flags == 3 and hr == 72 else None)
        assert fields['spo2'] == (98 if flags == 3 and spo2 == 98 else None)
        assert bytes.fromhex(' '.join(s['raw_hex'] for s in rx.lb_field_details(raw, fields))) == raw
    for broken in (GOLDEN[:-1], GOLDEN[:-2]+b'\x00\x00', b'XX'+GOLDEN[2:], GOLDEN[:9]+b'\x20'+GOLDEN[10:]):
        try:
            rx.parse_lb(broken)
        except ValueError:
            pass
        else:
            raise AssertionError('Malformed payload was accepted')

    app = rx.QtWidgets.QApplication.instance() or rx.QtWidgets.QApplication([])
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        settings = rx.QtCore.QSettings(str(temp/'settings.ini'), rx.QtCore.QSettings.IniFormat)
        original_discovery = rx.discover_rtl_devices
        rx.discover_rtl_devices = lambda: []
        try:
            window = rx.Window(settings)
        finally:
            rx.discover_rtl_devices = original_discovery
        window.log = temp/'receive.jsonl'
        # Ignore supplied cached fields, decode each row solely from its bytes.
        base = dict(time='TX golden sample', raw_payload_hex=GOLDEN.hex(' ').upper(),
                    phy_crc_valid=True, status='PHY CRC OK', fields={'device_id': 123})
        window.on_packet(base)
        assert window.records[0]['fields'] == EXPECTED
        assert window.table.item(0, rx.DISPLAY_KEYS.index('crc16_ccitt_le')).text() == '0xBC16'
        assert window.table.item(0, rx.DISPLAY_KEYS.index('hr_raw')).text() == '0xFF'
        assert window.table.item(0, rx.DISPLAY_KEYS.index('hr')).text() == '—'
        column = rx.DISPLAY_KEYS.index('message_type')
        assert window.table.item(0, column).text() == '1 (beacon)'
        window.set_language('zh')
        assert window.table.item(0, column).text() == '1 (信标)'
        window.set_language('en')
        assert window.table.item(0, column).text() == '1 (beacon)'
        window.on_packet(dict(base, raw_payload_hex='00 FF'))
        assert window.records[-1]['fields'] is None and 'LB REJECT' in window.records[-1]['status']
        window.on_packet(dict(base, phy_crc_valid=False, status='PHY CRC FAIL'))
        assert len(window.records) == 2
        stored = [json.loads(line) for line in window.log.read_text(encoding='utf-8').splitlines()]
        assert stored[0]['record_type'] == 'receiver_config'
        stored = [row for row in stored if row['record_type'] == 'packet']
        assert rx.parse_lb(bytes.fromhex(stored[0]['raw_payload_hex'])) == stored[0]['fields'] == EXPECTED
        assert stored[0]['payload_fields'] == details
        assert stored[0]['message_type_label'] == 'beacon'

        original_dialog = rx.QtWidgets.QFileDialog.getSaveFileName
        try:
            rx.QtWidgets.QFileDialog.getSaveFileName = lambda *args: (str(temp/'export.csv'), '')
            window.export_csv()
        finally:
            rx.QtWidgets.QFileDialog.getSaveFileName = original_dialog
        with (temp/'export.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = [row for row in csv.DictReader(stream) if row['record_type'] == 'packet']
        assert rows[0]['hr_raw'] == '255' and rows[0]['hr'] == ''
        assert rows[0]['crc16_ccitt_le'] == '48150' and rows[0]['name_length'] == '9'
        assert json.loads(rows[0]['payload_fields_json']) == details
        assert rows[0]['message_type'] == '1' and rows[0]['message_type_label'] == 'beacon'
        dialog_text = []

        def close_details():
            dialog = app.activeModalWidget()
            assert dialog is not None
            dialog_text.append(dialog.findChild(rx.QtWidgets.QPlainTextEdit).toPlainText())
            dialog.reject()

        rx.QtCore.QTimer.singleShot(50, close_details)
        window.show_packet_fields(0)
        assert '[rx] magic' in dialog_text[0] and '[28-29]' in dialog_text[0]
        assert '16 BC' in dialog_text[0] and 'DEMO2_PCB' in dialog_text[0]
        assert '1 (beacon)' in dialog_text[0]
        for message_type, english, chinese in [(0, 'CQ', '广泛呼叫'), (2, 'response', '应答')]:
            body = GOLDEN[:3] + bytes([message_type]) + GOLDEN[4:-2]
            raw = body + binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')
            window.on_packet(dict(base, raw_payload_hex=raw.hex(' ')))
            row = window.table.rowCount()-1
            assert window.table.item(row, column).text() == f'{message_type} ({english})'
            window.set_language('zh')
            assert window.table.item(row, column).text() == f'{message_type} ({chinese})'
            window.set_language('en')
            assert window.table.item(row, column).text() == f'{message_type} ({english})'
        window.close()
    print('PASS: supplied TX packet, all 17 byte fields, raw FF, CRC, standalone JSONL/CSV and field dialog')


def audit_logs():
    """Read-only audit of historical files; missing new fields in old schemas are allowed."""
    counts, reasons = Counter(), Counter()
    for path in (rx.ROOT/'logs').glob('*.jsonl'):
        counts['files'] += 1
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get('record_type') == 'receiver_config':
                    counts['config_headers'] += 1
                    continue
                counts['rows'] += 1
                raw = bytes.fromhex(record['raw_payload_hex'])
            except (ValueError, TypeError, KeyError) as exc:
                counts['unreadable_rows'] += 1
                reasons[type(exc).__name__] += 1
                continue
            try:
                fields = rx.parse_lb(raw)
            except ValueError as exc:
                counts['lb_rejected'] += 1
                reasons[str(exc)] += 1
                continue
            counts['standalone_lb_ok'] += 1
            counts['phy_verified_lb_ok'] += int(rx.phy_crc_passed(record))
            saved = record.get('fields') or {}
            if any(saved[key] != fields[key] for key in saved.keys() & fields.keys()):
                counts['stored_value_mismatch'] += 1
            if not set(EXPECTED).issubset(saved):
                counts['legacy_rows_missing_new_fields'] += 1
    print(json.dumps(dict(audit=dict(counts), rejection_reasons=dict(reasons)), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-logs', action='store_true')
    args = parser.parse_args()
    check_protocol()
    if args.audit_logs:
        audit_logs()
