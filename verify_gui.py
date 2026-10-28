"""GUI/export smoke check using synthetic data; does not open RTL hardware."""
import csv
import json
import tempfile
from pathlib import Path

import receiver as rx

app = rx.QtWidgets.QApplication([])
app.setFont(rx.QtGui.QFont('Segoe UI', 9))
settings_temp = tempfile.TemporaryDirectory()
settings = rx.QtCore.QSettings(str(Path(settings_temp.name)/'preferences.ini'), rx.QtCore.QSettings.IniFormat)
original_discovery = rx.discover_rtl_devices
rx.discover_rtl_devices = lambda: [('rtl=0', 'Test RTL A'), ('rtl=1', 'Test RTL B')]
window = rx.Window(settings)
window.set_language('en')
assert window.entries['scan_bandwidth'].text() == '1000000'
assert window.entries['sample_rate'].text() == '2400000'
frequencies = [433775000+offset for offset in rx.scan_offsets(433775000, 1000000, 125000)]
assert 433375000 in frequencies
assert all(b-a == 25000 for a,b in zip(frequencies, frequencies[1:]))
# Keep the synthetic UI smoke test light; defaults are checked above.
window.entries['scan_bandwidth'].setText('250000')
window.entries['sample_rate'].setText('1000000')
window.display_config = dict(order=rx.DISPLAY_KEYS[:], hidden=[], bold=[], color='alternate', show_failed=True)
window.apply_display_config()
original_receiver = rx.Receiver
receiver_params = []


def simulated_receiver(params, callback):
    receiver_params.append(dict(params))
    rng = rx.np.random.default_rng(7)
    noise = (rng.normal(size=32768) + 1j*rng.normal(size=32768))*0.01
    source = rx.blocks.vector_source_c(noise.tolist(), True)
    throttle = rx.blocks.throttle(rx.gr.sizeof_gr_complex, params['sample_rate'], True)
    receiver = original_receiver(params, callback, True, throttle)
    receiver.connect(source, throttle)
    receiver.test_source = source
    return receiver


rx.Receiver = simulated_receiver
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    window.log = temp/'live.jsonl'
    window.show()
    app.processEvents()
    assert window.advanced_panel.isVisible()
    assert all(entry.isVisible() for entry in window.entries.values())
    assert window.sync_verify.isChecked()
    window.device_combo.setCurrentIndex(0)
    assert not window.start_button.isEnabled() and not window.start_action.isEnabled()
    window.start()
    assert window.tb is None and 'Select a receive device' in window.state.text()
    assert not receiver_params
    window.device_combo.setCurrentIndex(window.device_combo.findData('rtl=1'))
    assert window.device == 'rtl=1'
    window.refresh_devices()
    assert window.device_combo.currentData() == 'rtl=1'
    # Manual arguments remain selectable even when the USB list does not include them.
    rx.discover_rtl_devices = lambda: []
    window.refresh_devices()
    assert window.device_combo.currentData() == 'rtl=1'
    assert 'not detected' in window.device_combo.currentText()
    rx.discover_rtl_devices = lambda: [('rtl=0', 'Test RTL A'), ('rtl=1', 'Test RTL B')]
    window.refresh_devices()
    window.device_combo.setCurrentIndex(window.device_combo.findData('rtl=0'))
    window.start()
    assert window.tb is not None and len(window.plots) == 2
    header = json.loads(window.log.read_text(encoding='utf-8').splitlines()[0])
    assert header['record_type'] == 'receiver_config' and header['event'] == 'reception_started'
    assert header['receiver_config']['sample_rate_sps'] == 1000000
    assert header['receiver_config']['scan_bandwidth_hz'] == 250000
    assert header['receiver_config']['sync_word_hex'] == '0x12'
    assert header['receiver_config']['sync_word_mode'] == 'strict'
    assert len(window.log.read_text(encoding='utf-8').splitlines()) == 1 and not window.records
    assert receiver_params[-1]['device'] == window.device_combo.currentData() == 'rtl=0'
    assert not window.configure_action.isEnabled()
    assert not window.device_combo.isEnabled() and not window.refresh_button.isEnabled()
    records = json.loads((rx.ROOT/'self-test-result.json').read_text())['records']
    for record in records:
        record['channel_frequency_hz'] = 433775000
        window.on_packet(record)
    window.on_packet(dict(time='test', raw_payload_hex='00 FF', status='PHY CRC OK / LB REJECT: LB magic/length', fields=None))
    status_column = 2 + len(rx.DECODE_COLUMNS)
    assert window.table.horizontalHeaderItem(status_column).text() == 'Status'
    assert window.table.item(1, status_column+1).text() == '-6.02'
    assert window.table.item(4, status_column+1).text() == '—'
    assert window.table.item(1, 0).background().color().name() == '#eeeeee'
    assert window.table.columnCount() == len(rx.DISPLAY_KEYS)
    frequency_column = rx.DISPLAY_KEYS.index('channel_frequency_hz')
    assert window.table.item(0, frequency_column).text() == '433775000'
    window.display_config.update(order=list(reversed(rx.DISPLAY_KEYS)), hidden=['raw_payload_hex'],
                                 bold=['device_id'], color='device', show_failed=False)
    window.apply_display_config()
    assert window.table.rowCount() == 4 and len(window.records) == 5
    assert window.table.isColumnHidden(1)
    assert window.table.horizontalHeader().visualIndex(frequency_column) == 0
    assert window.table.item(0, 2).font().bold()
    assert window.table.item(0, 0).background().color() == window.table.item(1, 0).background().color()
    window.display_config.update(color='white', show_failed=True)
    window.apply_display_config()
    assert window.table.rowCount() == 5
    assert window.table.item(1, 0).background().color().name() == '#ffffff'
    assert window.splitter.count() == 2
    window.set_language('zh')
    assert window.menu.actions()[0].text() == '设备'
    assert [a.text() for a in window.menu.actions()] == ['设备', '日志', '可视化', '帮助', '语言']
    assert window.export_menu.title() == '导出当前会话'
    assert window.exit_action in window.device_menu.actions()
    assert window.exit_action not in window.logs_menu.actions()
    assert window.device_combo.currentText() == 'Test RTL A · rtl=0'
    assert window.spectrum_scale.text().startswith('强度：')
    assert window.waterfall_scale.text().endswith('最新采样在底部')
    assert window.table.horizontalHeaderItem(rx.DISPLAY_KEYS.index('signal_avg_dbfs')).text() == '平均强度 (dBFS)'
    assert window.entries['gain'].isEnabled()
    applied_gains = []
    window.tb.set_gain = applied_gains.append
    window.gain_slider.setValue(35)
    assert applied_gains[-1] == 35 and window.entries['gain'].text() == '35'
    assert window.config_history[-1]['event'] == 'gain_changed'
    assert window.config_history[-1]['receiver_config']['gain_requested_db'] == 35
    window.entries['gain'].setText('28.5')
    window.apply_gain_entry()
    assert applied_gains[-1] == 28.5 and window.entries['gain'].text() == '28.5'
    window.entries['gain'].setText('999')
    window.apply_gain_entry()
    assert applied_gains[-1] == 28.5
    window.entries['gain'].setText('28.5')
    # Dense nearby devices require more than the old two alternating label rows.
    view = window.distance_window
    view.devices.clear()
    for i in range(7):
        view.update_device(dict(fields=dict(device_id=i+1, name=f'DEVICE {i+1}'), signal_avg_dbfs=-30-i*0.01))
    for width in (380, 1000):
        view.resize(width, view.minimumHeight())
        for mode in (0, 1):
            view.axis_mode.setCurrentIndex(mode)
            view.refresh_label_layout()
            labels, lanes = view.device_label_layout()
            assert lanes >= 7
            assert all(label['rect'].top() >= 44 for label in labels)
            assert all(not a['rect'].intersects(b['rect']) for i,a in enumerate(labels) for b in labels[i+1:])
    # New signal strength changes marker position and repacks label heights.
    old_x = {label['device']['id']: label['x'] for label in labels}
    view.update_device(dict(fields=dict(device_id=1, name='DEVICE 1'), signal_avg_dbfs=-60))
    assert len(view.devices) == 7
    labels, _ = view.device_label_layout()
    assert next(label['x'] for label in labels if label['device']['id'] == 1) != old_x[1]
    window.visual_action.setChecked(False)
    assert window.distance_panel.isHidden()
    window.visual_action.setChecked(True)
    assert not window.distance_panel.isHidden()
    # Use a representative three-device layout for the visual preview.
    view.devices.clear()
    for i, strength in enumerate((-28, -29, -50)):
        view.update_device(dict(fields=dict(device_id=0x999990+i, name=f'DEMO {i+1}'), signal_avg_dbfs=strength))
    view.axis_mode.setCurrentIndex(0)
    window.set_language('en')
    original_dialog = rx.QtWidgets.QFileDialog.getSaveFileName
    try:
        rx.QtWidgets.QFileDialog.getSaveFileName = lambda *args: (str(temp/'export.jsonl'), '')
        window.export_jsonl()
        assert len((temp/'export.jsonl').read_text().splitlines()) == 5+len(window.config_history)
        rx.QtWidgets.QFileDialog.getSaveFileName = lambda *args: (str(temp/'export.csv'), '')
        window.export_csv()
        with (temp/'export.csv').open(encoding='utf-8-sig', newline='') as stream:
            exported = [row for row in csv.DictReader(stream) if row['record_type'] == 'packet']
        assert len(exported) == 5
        assert exported[1]['signal_avg_dbfs'] == str(records[1]['signal_avg_dbfs'])
        assert exported[0]['name'] == 'Lambert CHEN'
        assert exported[0]['magic'] == 'LB' and exported[0]['version'] == '1'
        assert len(json.loads(exported[0]['payload_fields_json'])) == 17
    finally:
        rx.QtWidgets.QFileDialog.getSaveFileName = original_dialog
    window.state.setText('SYNTHETIC UI CHECK — not live RF')
    loop = rx.QtCore.QEventLoop()
    rx.QtCore.QTimer.singleShot(1200, loop.quit)
    loop.exec_()
    window.grab().save(str(rx.ROOT/'layout-preview.png'))
    window.stop()
    assert window.configure_action.isEnabled() and not window.plots
    assert window.device_combo.isEnabled() and window.refresh_button.isEnabled()
    window.close()
rx.discover_rtl_devices = original_discovery
settings_temp.cleanup()
print('PASS: menus, device selection, visible RF settings, live gain, adaptive labels, exports, start/stop')
