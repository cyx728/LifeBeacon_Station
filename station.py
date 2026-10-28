"""LifeBeacon Station: RTL receive and HackRF half-duplex receive/transmit."""
import binascii
import ctypes
import ctypes.util
import gc
import json
import sys
import time
from datetime import datetime
from pathlib import Path
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from gnuradio import blocks, gr, lora_sdr
import osmosdr
import receiver as core


def cq_packet(device_id):
    if not 0 <= device_id <= 0xffffffff:
        raise ValueError('Device ID must be uint32')
    body = b'LB\x01' + device_id.to_bytes(4, 'little') + b'\x00'
    return body + binascii.crc_hqx(body, 0xffff).to_bytes(2, 'little')


_rtl_discovery = core.discover_rtl_devices
def discover_devices():
    devices = []
    try:
        devices.extend(_rtl_discovery())
    except Exception:
        pass
    # libhackrf enumeration does not open or transmit through a device.
    class DeviceList(ctypes.Structure):
        _fields_ = [('serial_numbers', ctypes.POINTER(ctypes.c_char_p)),
                    ('usb_board_ids', ctypes.POINTER(ctypes.c_int)),
                    ('usb_device_index', ctypes.POINTER(ctypes.c_int)),
                    ('devicecount', ctypes.c_int), ('usb_devices', ctypes.c_void_p),
                    ('usb_devicecount', ctypes.c_int)]
    library = str(Path(sys.prefix)/'Library/bin/hackrf-0.dll') if sys.platform == 'win32' else ctypes.util.find_library('hackrf')
    if library:
        lib = ctypes.CDLL(library)
        lib.hackrf_device_list.restype = ctypes.POINTER(DeviceList)
        lib.hackrf_device_list_free.argtypes = [ctypes.POINTER(DeviceList)]
        if lib.hackrf_init() != 0:
            return devices
        listing = lib.hackrf_device_list()
        try:
            if listing:
                for i in range(listing.contents.devicecount):
                    raw_serial = listing.contents.serial_numbers[i]
                    if not raw_serial:
                        continue  # busy/inaccessible devices may have no serial descriptor
                    serial = raw_serial.decode('ascii')
                    devices.append(('hackrf='+serial, 'HackRF / Black SDR HackRF Pro'))
        finally:
            if listing:
                lib.hackrf_device_list_free(listing)
            lib.hackrf_exit()
    return devices


def generate_iq(raw, sf=7, bw=125000, rate=2000000, sync=0x12, cr=1):
    if bw not in (125000, 250000, 500000) or rate % bw or not 7 <= sf <= 12 or not 1 <= cr <= 4:
        raise ValueError('Invalid LoRa modulation parameters')
    graph = gr.top_block()
    source = blocks.vector_source_b(list(raw), False)
    tagged = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, len(raw), 'packet_len')
    whitening = lora_sdr.whitening(False, True, ',', 'packet_len')
    header = lora_sdr.header(False, True, cr)
    crc = lora_sdr.add_crc(True)
    hamming = lora_sdr.hamming_enc(cr, sf)
    interleave = lora_sdr.interleaver(cr, sf, 2, bw)
    gray = lora_sdr.gray_demap(sf)
    modulate = lora_sdr.modulate(sf, rate, bw, [sync], int(rate*.01), 8)
    sink = blocks.vector_sink_c()
    graph.connect(source, tagged, whitening, header, crc, hamming, interleave, gray, modulate, sink)
    graph.run()
    data = np.asarray(sink.data(), dtype=np.complex64)*0.5
    if not len(data):
        raise RuntimeError('LoRa encoder produced no samples')
    return np.concatenate((np.zeros(int(rate*.01), np.complex64), data,
                           np.zeros(int(rate*.02), np.complex64)))


class PrepareTransmission(QtCore.QThread):
    result = QtCore.pyqtSignal(object)

    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.params = params

    def run(self):
        try:
            p = self.params
            started = time.monotonic()
            iq = generate_iq(p['raw'], p['sf'], p['bw'], p['rate'], p['sync'], p['cr'])
            # Conversion is deliberately completed while RX is still running.
            samples = iq.tolist()
            self.result.emit(dict(iq=samples, duration_s=len(samples)/p['rate'],
                                  prepare_s=time.monotonic()-started))
        except Exception as exc:
            self.result.emit(dict(error=str(exc)))


class Transmitter(QtCore.QThread):
    result = QtCore.pyqtSignal(object)
    def __init__(self, params, parent=None):
        super().__init__(parent)
        self.params = params
        self.graph = None

    def run(self):
        p = self.params
        try:
            iq = p['iq']
            if self.isInterruptionRequested():
                raise RuntimeError('Cancelled')
            self.graph = gr.top_block()
            sink = osmosdr.sink(args='numchan=1 '+p['device'])
            sink.set_sample_rate(p['rate'])
            sink.set_center_freq(p['frequency'])
            if abs(sink.get_sample_rate()-p['rate']) > 1 or abs(sink.get_center_freq()-p['frequency']) > 1:
                raise RuntimeError('HackRF TX sample rate / frequency readback mismatch')
            sink.set_gain(0, 'RF')
            sink.set_gain(p['gain'], 'IF')
            if sink.get_gain('RF') != 0 or sink.get_gain('IF') != p['gain']:
                raise RuntimeError('HackRF TX gain readback mismatch')
            sink.set_bandwidth(p['bw'])
            source = blocks.vector_source_c(iq, False)
            self.graph.connect(source, sink)
            if self.isInterruptionRequested():
                raise RuntimeError('Cancelled')
            self.graph.start()
            # gr-osmosdr's sink does not reliably finish on finite-source EOF.
            # Its stop() flushes USB queues. Do not wait for EOF before stopping.
            deadline = time.monotonic()+len(iq)/p['rate']+0.10
            while not self.isInterruptionRequested() and time.monotonic() < deadline:
                self.msleep(5)
            cancelled = self.isInterruptionRequested()
            self.graph.stop()
            self.graph.wait()
            self.graph = None
            del source, sink
            gc.collect()
            self.result.emit(dict(ok=not cancelled, status='cancelled' if cancelled else 'hardware_stream_completed',
                                  samples=len(iq), duration_s=len(iq)/p['rate']))
        except Exception as exc:
            if self.graph:
                self.graph.stop()
                self.graph.wait()
                self.graph = None
            gc.collect()
            self.result.emit(dict(ok=False, status=str(exc)))

    def cancel(self):
        self.requestInterruption()


class PauseBadge(QtWidgets.QLabel):
    """Overlay the retained plot without replacing it or clearing its history."""
    def __init__(self, plot_group):
        super().__init__(plot_group)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet('background: rgba(30, 35, 42, 210); color: white; '
                           'border-radius: 8px; padding: 10px 24px; font-size: 18px; font-weight: 600;')
        plot_group.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self.center()
        return super().eventFilter(obj, event)

    def center(self):
        self.adjustSize()
        rect = self.parentWidget().contentsRect()
        self.move(rect.center()-self.rect().center())


class Station(core.Window):
    def __init__(self):
        self.tx_worker = None
        self.tx_preparer = None
        self.prepared_tx = None
        self.rx_paused = False
        self.tx_cancelled = False
        self.last_tx_result = None
        self.close_after_tx = False
        self.resume_rx = False
        self.tx_panel = None
        super().__init__(preferences=QtCore.QSettings('LifeBeacon', 'Station'))
        self.pause_badges = [PauseBadge(self.spectrum_group), PauseBadge(self.waterfall_group)]
        self.tx_panel = QtWidgets.QGroupBox()
        form = QtWidgets.QFormLayout(self.tx_panel)
        self.tx_fields = {}
        for key, value in [('device_id', '0x00000001'), ('frequency', '433375000'), ('bw', '125000'),
                           ('sf', '7'), ('cr', '1'), ('sync', '0x12'), ('rate', '2000000'), ('gain', '0')]:
            edit = QtWidgets.QLineEdit(str(self.preferences.value('tx_'+key, value)))
            self.tx_fields[key] = edit
            label = QtWidgets.QLabel()
            form.addRow(label, edit)
        self.tx_preview = QtWidgets.QLineEdit()
        self.tx_preview.setReadOnly(True)
        form.addRow(self.tx_preview)
        self.send_button = QtWidgets.QPushButton()
        self.cancel_button = QtWidgets.QPushButton()
        self.send_button.clicked.connect(self.send_packet)
        self.cancel_button.clicked.connect(self.cancel_tx)
        self.cancel_button.setEnabled(False)
        form.addRow(self.send_button, self.cancel_button)
        self.tx_status = QtWidgets.QLabel()
        self.tx_status.setWordWrap(True)
        form.addRow(self.tx_status)
        self.tx_fields['device_id'].textChanged.connect(self.preview)
        self.splitter.addWidget(self.tx_panel)
        self.splitter.setSizes([400, 1000, 300])
        self.hackrf_controls = QtWidgets.QWidget()
        gains = QtWidgets.QFormLayout(self.hackrf_controls)
        self.lna = QtWidgets.QSpinBox(); self.lna.setRange(0, 40); self.lna.setSingleStep(8); self.lna.setValue(16)
        self.vga = QtWidgets.QSpinBox(); self.vga.setRange(0, 62); self.vga.setSingleStep(2); self.vga.setValue(20)
        self.amp = QtWidgets.QCheckBox('RF AMP (+14 dB)')
        gains.addRow('HackRF LNA (dB)', self.lna)
        gains.addRow('HackRF VGA (dB)', self.vga)
        gains.addRow(self.amp)
        self.advanced_panel.layout().addRow(self.hackrf_controls)
        self.set_language(self.language)
        self.select_device(self.device_combo.currentIndex())
        self.preview()

    def preview(self):
        try:
            self.tx_preview.setText(cq_packet(int(self.tx_fields['device_id'].text(), 0)).hex(' ').upper())
        except ValueError:
            self.tx_preview.clear()

    def select_device(self, index):
        super().select_device(index)
        if self.tx_panel is not None:
            hackrf = str(self.device_combo.currentData()).startswith('hackrf=')
            self.tx_panel.setVisible(hackrf)
            self.hackrf_controls.setVisible(hackrf)
            self.entries['gain'].setEnabled(not hackrf)
            self.gain_slider.setEnabled(not hackrf)

    def set_language(self, language):
        super().set_language(language)
        self.setWindowTitle('LifeBeacon Station — RTL-SDR / HackRF Pro')
        self.form_labels['sample_rate'].setText('采样率 (S/s)' if language == 'zh' else 'Sample rate (S/s)')
        self.spectrum_group.setTitle('频谱图' if language == 'zh' else 'Spectrum')
        self.waterfall_group.setTitle('瀑布图' if language == 'zh' else 'Waterfall')
        self.sync_verify.setText('严格校验同步字' if language == 'zh' else 'Verify sync word')
        # Keep these technical notes available on hover instead of in the layout.
        self.scan_description.hide()
        self.advanced_panel.setToolTip(self.scan_description.text())
        self.spectrum_scale.hide()
        self.waterfall_scale.hide()
        self.spectrum_group.setToolTip(self.spectrum_scale.text())
        self.waterfall_group.setToolTip(self.waterfall_scale.text())
        self.set_plot_paused(self.rx_paused)
        if self.tx_panel is None:
            return
        zh = language == 'zh'
        self.tx_panel.setTitle('发送 CQ（半双工）' if zh else 'Transmit CQ (half duplex)')
        labels = (['设备 ID', '频率 (Hz)', '带宽 (Hz)', '扩频因子', '编码率 (1–4)', '同步字', '采样率 (S/s)', 'TX VGA (dB)']
                  if zh else ['Device ID', 'Frequency (Hz)', 'Bandwidth (Hz)', 'SF', 'Coding rate (1–4)', 'Sync word', 'Sample rate (S/s)', 'TX VGA (dB)'])
        for edit, label in zip(self.tx_fields.values(), labels):
            self.tx_panel.layout().labelForField(edit).setText(label)
        self.send_button.setText('发送一次' if zh else 'Send once')
        self.cancel_button.setText('取消' if zh else 'Cancel')

    def set_plot_paused(self, paused):
        for badge in getattr(self, 'pause_badges', []):
            badge.setText('暂停' if self.language == 'zh' else 'Paused')
            badge.center()
            badge.setVisible(paused)
            if paused:
                badge.raise_()
                badge.repaint()

    def configure_device(self):
        value, ok = QtWidgets.QInputDialog.getText(self, 'Device', 'rtl=0 / hackrf=<serial>', text=self.device)
        if ok:
            value = value.strip()
            if not (value.startswith('rtl=') or value.startswith('hackrf=')) or ',' in value or ' ' in value:
                self.state.setText('Invalid device argument')
                return
            self.device = value
            self.refresh_devices()
            self.device_combo.setCurrentIndex(self.device_combo.findData(value))

    def start(self):
        if self.tx_worker is not None or self.tx_preparer is not None:
            return
        super().start()

    def hardware_params(self):
        if not self.device.startswith('hackrf='):
            return {}
        if self.lna.value() % 8 or self.vga.value() % 2:
            raise ValueError('HackRF LNA uses 8 dB steps; VGA uses 2 dB steps')
        return dict(hackrf_lna=self.lna.value(), hackrf_vga=self.vga.value(), hackrf_amp=self.amp.isChecked())

    def update_reception_controls(self):
        super().update_reception_controls()
        self.sync_verify.setEnabled(self.tb is None and self.tx_worker is None)
        if self.tx_panel is not None:
            hackrf = str(self.device_combo.currentData()).startswith('hackrf=')
            self.entries['gain'].setEnabled(not hackrf)
            self.gain_slider.setEnabled(not hackrf)
            self.hackrf_controls.setEnabled(self.tb is None and self.tx_worker is None)
        if self.tx_worker is not None or self.tx_preparer is not None:
            for obj in (self.config_group, self.start_action, self.stop_action, self.configure_action, self.refresh_action):
                obj.setEnabled(False)

    def send_packet(self):
        if self.tx_worker is not None or self.tx_preparer is not None:
            return
        try:
            device = str(self.device_combo.currentData())
            if not device.startswith('hackrf='):
                raise ValueError('Select HackRF first')
            p = {k: int(v.text(), 0) for k, v in self.tx_fields.items()}
            if not 1000000 <= p['frequency'] <= 6000000000 or p['rate'] not in (2000000, 4000000, 8000000):
                raise ValueError('Frequency / sample rate out of range')
            if p['gain'] % 1 or not 0 <= p['gain'] <= 47 or not 0 <= p['sync'] <= 255:
                raise ValueError('Invalid TX gain / sync')
            if p['bw'] not in (125000, 250000, 500000) or not 7 <= p['sf'] <= 12 or not 1 <= p['cr'] <= 4:
                raise ValueError('Invalid LoRa parameters')
            p.update(raw=cq_packet(p['device_id']), device=device)
        except ValueError as exc:
            self.tx_status.setText(str(exc)); return
        self.tx_params = p
        self.resume_rx = False
        self.tx_cancelled = False
        self.last_tx_result = None
        for k, edit in self.tx_fields.items():
            self.preferences.setValue('tx_'+k, edit.text())
            edit.setEnabled(False)
        self.send_button.setEnabled(False); self.cancel_button.setEnabled(True)
        self.tx_status.setText('准备报文（继续接收）…' if self.language == 'zh' else 'Preparing packet (RX continues)…')
        self.tx_preparer = PrepareTransmission(p, self)
        self.tx_preparer.result.connect(self.tx_prepared)
        self.tx_preparer.finished.connect(self.tx_preparation_finished)
        self.update_reception_controls()
        self.tx_preparer.start()

    def tx_prepared(self, result):
        self.prepared_tx = result

    def tx_preparation_finished(self):
        self.tx_preparer.deleteLater()
        self.tx_preparer = None
        prepared, self.prepared_tx = self.prepared_tx, None
        if self.tx_cancelled or not prepared or 'error' in prepared:
            self.tx_status.setText('cancelled' if self.tx_cancelled else (prepared or {}).get('error', 'Preparation failed'))
            self.tx_finished()
            return
        self.resume_rx = self.tb is not None
        self.rx_pause_started = time.monotonic()
        self.tx_params['prepare_s'] = prepared['prepare_s']
        try:
            if self.resume_rx:
                # Release only USB source; retain decoders and exact plot widgets.
                self.rx_paused = True
                self.set_plot_paused(True)
                self.tb.release_radio()
                gc.collect()
            self.tx_worker = Transmitter(dict(self.tx_params, iq=prepared['iq']), self)
        except Exception as exc:
            self.tx_result(dict(ok=False, status=str(exc)))
            self.tx_finished()
            return
        self.tx_worker.result.connect(self.tx_result)
        self.tx_worker.finished.connect(self.tx_finished)
        self.tx_status.setText('发送中（接收暂停）…' if self.language == 'zh' else 'Transmitting (RX paused)…')
        self.update_reception_controls()
        self.tx_worker.start()

    def tx_result(self, result):
        self.last_tx_result = dict(result)
        self.tx_status.setText(result['status'])

    def tx_finished(self):
        if self.tx_worker is not None:
            self.tx_worker.deleteLater(); self.tx_worker = None
        if self.rx_paused:
            try:
                self.tb.restore_radio()
                self.last_rx_pause_s = time.monotonic()-self.rx_pause_started
                if self.last_tx_result is not None:
                    self.last_tx_result['rx_pause_s'] = self.last_rx_pause_s
                self.state.setText(('接收已恢复，暂停 ' if self.language == 'zh' else 'RX resumed; paused ')
                                   + f'{self.last_rx_pause_s:.3f} s')
            except Exception as exc:
                # Leave Start usable when reopening the USB source fails.
                core.Window.stop(self)
                self.state.setText('RX resume failed: '+str(exc))
                if self.last_tx_result is not None:
                    self.last_tx_result['rx_resume_error'] = str(exc)
            finally:
                self.rx_paused = False
                self.set_plot_paused(False)
        if self.last_tx_result is not None:
            row = dict(record_type='transmit', time=datetime.now().astimezone().isoformat(),
                       raw_payload_hex=self.tx_params['raw'].hex(' ').upper(),
                       configuration={k: v for k, v in self.tx_params.items() if k != 'raw'}, **self.last_tx_result)
            try:
                with (core.ROOT/'transmit-history.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(row)+'\n')
            except OSError as exc:
                self.tx_status.setText('TX log: '+str(exc))
        self.config_group.setEnabled(True)
        for edit in self.tx_fields.values(): edit.setEnabled(True)
        self.send_button.setEnabled(True); self.cancel_button.setEnabled(False)
        self.update_reception_controls()
        self.resume_rx = False
        self.select_device(self.device_combo.currentIndex())
        if self.close_after_tx:
            self.close()

    def cancel_tx(self):
        self.tx_cancelled = True
        if self.tx_worker is not None: self.tx_worker.cancel()

    def closeEvent(self, event):
        if self.tx_worker is not None or self.tx_preparer is not None:
            self.close_after_tx = True
            self.resume_rx = False
            self.cancel_tx()
            event.ignore()
            return
        super().closeEvent(event)


core.discover_rtl_devices = discover_devices

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont('Segoe UI', 9))
    window = Station()
    window.show()
    if '--gui-smoke-test' in sys.argv:
        QtCore.QTimer.singleShot(1500, app.quit)
    sys.exit(app.exec_())
