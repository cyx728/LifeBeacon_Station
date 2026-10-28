"""Log header/changes and packet references survive JSONL/CSV import without counting as packets."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile

import receiver as rx
from log_analysis import read_log_files, range_statistics


app = rx.QtWidgets.QApplication([])
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    settings = rx.QtCore.QSettings(str(temp/'settings.ini'), rx.QtCore.QSettings.IniFormat)
    original_discovery = rx.discover_rtl_devices
    rx.discover_rtl_devices = lambda: [('rtl=0', 'Synthetic test device')]
    try:
        window = rx.Window(settings)
    finally:
        rx.discover_rtl_devices = original_discovery
    window.log = temp/'session.jsonl'
    p = window.configured_params()
    first = window.record_receiver_configuration(p, 'reception_started')
    config = first['receiver_config']
    assert config['center_frequency_hz'] == 433775000
    assert config['sample_rate_sps'] == 2400000 and config['scan_bandwidth_hz'] == 1000000
    assert config['lora_bandwidth_hz'] == 125000 and config['spreading_factors'] == [7, 8, 9, 10, 11, 12]
    assert config['sync_word_hex'] == '0x12' and config['gain_readback_db'] is None
    assert config['device'] == 'rtl=0' and config['device_name'] == 'Synthetic test device'
    assert len(window.records) == 0 and len(window.log.read_text().splitlines()) == 1
    packet = json.loads((rx.ROOT/'self-test-result.json').read_text())['records'][0]
    window.on_packet(packet)
    assert window.records[0]['receiver_config_id'] == first['config_id']
    # Simulate a device selecting a supported gain different from the requested value.
    window.tb = SimpleNamespace(source=SimpleNamespace(get_gain=lambda: 29.7),
                                 set_gain=lambda value: None, stop=lambda: None, wait=lambda: None)
    window.apply_gain(30)
    second = window.config_history[-1]
    assert second['event'] == 'gain_changed' and second['config_id'] != first['config_id']
    assert second['receiver_config']['gain_readback_db'] == 29.7
    assert second['receiver_config']['gain_requested_db'] == 30
    window.on_packet(packet)
    assert window.records[-1]['receiver_config_id'] == second['config_id']
    window.stop()
    # Restart with changed center frequency produces another configuration, not a new packet.
    window.entries['frequency'].setText('433375000')
    third = window.record_receiver_configuration(window.configured_params(), 'reception_started')
    window.on_packet(packet)
    assert third['receiver_config']['center_frequency_hz'] == 433375000
    rows = [json.loads(line) for line in window.log.read_text().splitlines()]
    assert [row['record_type'] for row in rows] == ['receiver_config', 'packet']*3
    assert len(window.records) == window.table.rowCount() == 3
    original_dialog = rx.QtWidgets.QFileDialog.getSaveFileName
    try:
        rx.QtWidgets.QFileDialog.getSaveFileName = lambda *args: (str(temp/'export.jsonl'), '')
        window.export_jsonl()
        rx.QtWidgets.QFileDialog.getSaveFileName = lambda *args: (str(temp/'export.csv'), '')
        window.export_csv()
    finally:
        rx.QtWidgets.QFileDialog.getSaveFileName = original_dialog
    with (temp/'export.csv').open(encoding='utf-8-sig', newline='') as stream:
        csv_rows = list(csv.DictReader(stream))
    assert csv_rows[0]['record_type'] == 'receiver_config'
    for path in (window.log, temp/'export.jsonl', temp/'export.csv'):
        metadata = []
        packets, counts, issues = read_log_files([path], rx.decode_record, rx.phy_crc_passed, metadata)
        assert len(packets) == 3 and len(metadata) == 3 and not issues, (path, counts, issues)
        assert counts == dict(config_headers=3, input_rows=3, loaded=3, lb_ok=3)
        assert [r['receiver_config_id'] for r in packets] == [h['config_id'] for h in metadata]
        assert range_statistics(packets, 1, 3)['packets'] == 3
        browser = window.log_browser()
        browser.load_paths([path])
        assert browser.config_button.isEnabled() and browser.model.rowCount() == 3
        assert browser.end_input.maximum() == 3
        browser.close()
    window.close()
print('PASS: initial header, configuration changes, readback gain, packet references, JSONL/CSV roundtrip and statistics')
