"""Historical raw re-decoding, range/wrap semantics, missing data and GUI isolation."""
import binascii
import csv
import hashlib
import json
from pathlib import Path
import tempfile

import receiver as rx
from log_analysis import read_log_files, range_statistics


def packet(device, sequence, flags, hr, spo2, signal, peak=None):
    name = b'DEMO'
    body = (b'LB\x01\x01\x00'+device.to_bytes(4, 'little')+bytes([len(name)])+name+
            bytes([flags, hr, spo2, 0, 0, 50, sequence, 128, 0]))
    raw = body+binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')
    return dict(time='2026-10-26 12:00:00', raw_payload_hex=raw.hex(' '), phy_crc_valid=True,
                status='PHY CRC OK / LB OK', fields={'device_id': 999},
                signal_avg_dbfs=signal, signal_peak_dbfs=peak)


app = rx.QtWidgets.QApplication([])
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    source = temp/'old.jsonl'
    fixtures = [packet(1, 254, 3, 60, 96, -40, -35),
                packet(1, 255, 1, 80, 97, -20, -15),
                packet(1, 0, 3, 255, 98, None, -25),
                packet(2, 1, 0, 70, 95, -30, -25),
                packet(1, 1, 3, 100, 100, -30, -25)]
    # Repeat sequence in a later cycle is still a received packet, not deduplicated.
    fixtures.append(packet(1, 254, 3, 80, 96, -30, -25))
    fixtures += [dict(fixtures[0], raw_payload_hex='00 FF'),
                 dict(fixtures[0], phy_crc_valid=False), dict(fixtures[0], raw_payload_hex='ZZ')]
    source.write_text(''.join(json.dumps(row)+'\n' for row in fixtures)+'not json\n', encoding='utf-8')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    records, counts, issues = read_log_files([source], rx.decode_record, rx.phy_crc_passed)
    assert counts == dict(input_rows=10, loaded=7, lb_ok=6, lb_rejected=1, phy_dropped=1, malformed=2), counts
    assert len(issues) == 2 and records[0]['fields']['device_id'] == 1
    assert records[0]['fields']['rssi_placeholder'] == 128
    all_rows = range_statistics(records, 1, 7)
    assert (all_rows['devices'], all_rows['packets'], all_rows['excluded_rows']) == (2, 6, 1)
    first, second = all_rows['rows']
    assert first['packets'] == 5 and first['signal_valid'] == 4
    assert (first['signal_max'], first['signal_min'], first['signal_avg']) == (-20, -40, -30)
    assert first['hr_valid'] == 4 and first['hr_avg'] == 80 and first['hr_valid_percent'] == 80
    assert first['hr_values'] == '60, 80, 100, 80'
    assert first['spo2_valid'] == 4 and first['spo2_avg'] == 97.5
    assert second['hr_valid'] == second['spo2_valid'] == 0
    assert second['hr_avg'] is None and second['spo2_values'] == ''
    assert range_statistics(records, 255, 0, 'sequence')['packets'] == 2
    assert range_statistics(records, 254, 1, 'sequence')['packets'] == 6
    assert range_statistics(records, 1, 1, 'sequence')['packets'] == 2
    assert range_statistics(records, 254, 254, 'sequence')['packets'] == 2
    assert range_statistics(records, 1, 2)['rows'][0]['signal_avg'] == -30
    assert range_statistics(records, 1, 2, signal='signal_peak_dbfs')['rows'][0]['signal_avg'] == -25
    assert range_statistics(records, 20, 30)['devices'] == 0
    assert range_statistics([], 1, 1)['rows'] == []
    for start, end, mode in [(2, 1, 'row'), (-1, 20, 'sequence'), (0, 256, 'sequence')]:
        try:
            range_statistics(records, start, end, mode)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid range accepted')
    csv_path = temp/'old.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['time', 'raw_payload_hex', 'status', 'signal_avg_dbfs'])
        writer.writeheader()
        writer.writerow({key: fixtures[0][key] for key in writer.fieldnames})
    imported_csv, _, _ = read_log_files([csv_path], rx.decode_record, rx.phy_crc_passed)
    assert imported_csv[0]['signal_avg_dbfs'] == -40
    assert imported_csv[0]['fields']['device_id'] == 1
    flagged_csv = temp/'flags.csv'
    with flagged_csv.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['time', 'raw_payload_hex', 'status', 'phy_crc_valid', 'signal_avg_dbfs'])
        writer.writeheader()
        for flag in ('True', 'False', 'bad'):
            writer.writerow({key: fixtures[0].get(key) for key in writer.fieldnames} | {'phy_crc_valid': flag})
    flagged, flag_counts, _ = read_log_files([flagged_csv], rx.decode_record, rx.phy_crc_passed)
    assert len(flagged) == 1 and flag_counts['phy_dropped'] == 1 and flag_counts['malformed'] == 1
    combined, combined_counts, _ = read_log_files([source, csv_path], rx.decode_record, rx.phy_crc_passed)
    assert len(combined) == 8 and combined_counts['lb_ok'] == 7

    settings = rx.QtCore.QSettings(str(temp/'settings.ini'), rx.QtCore.QSettings.IniFormat)
    original_discovery = rx.discover_rtl_devices
    rx.discover_rtl_devices = lambda: []
    try:
        window = rx.Window(settings)
    finally:
        rx.discover_rtl_devices = original_discovery
    window.log = temp/'live.jsonl'
    window.on_packet(fixtures[0])
    live_bytes = window.log.read_bytes()
    for language in ('zh', 'en'):
        window.set_language(language)
        browser = window.log_browser()
        browser.load_paths([source])
        browser.show()
        app.processEvents()
        assert browser.model.rowCount() == 7 and browser.stats_table.rowCount() == 2
        assert browser.model.data(browser.model.index(0, rx.DISPLAY_KEYS.index('device_id'))) == '0x00000001'
        browser.range_mode.setCurrentIndex(1)
        browser.start_input.setValue(255)
        browser.end_input.setValue(0)
        assert browser.report['packets'] == 2
        browser.signal_mode.setCurrentIndex(1)
        browser.write_statistics(temp/'statistics.csv')
        with (temp/'statistics.csv').open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            assert len(reader.fieldnames) == len(set(reader.fieldnames))
        assert rows[0]['total_packets'] == rows[0]['packets'] == '2'
        assert rows[0]['signal_avg'] == '-20.0' and rows[0]['signal_field'] == 'signal_peak_dbfs'
        assert rows[0]['range_start'] == '255' and rows[0]['range_end'] == '0'
        assert len(window.records) == 1 and window.log.read_bytes() == live_bytes
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        if language == 'zh':
            browser.tabs.setCurrentIndex(1)
            app.processEvents()
            browser.grab().save(str(rx.ROOT/'log-statistics-preview.png'))
        browser.close()
    snapshot = window.log_browser(list(window.records))
    window.on_packet(fixtures[1])
    assert len(snapshot.records) == 1 and len(window.records) == 2
    snapshot.close()
    window.close()
print('PASS: JSONL/CSV import, payload recheck, inclusive row/sequence/wrap ranges, per-device stats, exports, UI isolation')
