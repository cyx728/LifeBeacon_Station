"""Live RX and failure/recovery integration; TX worker mocked, no transmission."""
import json
import tempfile
from pathlib import Path
import time
import sys
import station as s

app = s.QtWidgets.QApplication([])
app.setFont(s.QtGui.QFont('Segoe UI', 9))
w = s.Station(); w.show()
device = next(d for d, name in s.discover_devices() if d.startswith('hackrf='))
w.device_combo.setCurrentIndex(w.device_combo.findData(device))
w.entries['frequency'].setText('433375000')
w.entries['scan_bandwidth'].setText('1000000' if '--wide' in sys.argv else '125000')
def pump(seconds):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(.01)
w.start(); assert w.tb is not None, w.state.text()
original_tb = w.tb
original_waterfall = w.tb.waterfall
original_plots = list(w.plots)
pump(2)
waterfall_samples_before = w.tb.waterfall.nitems_read(0)
assert waterfall_samples_before > 0
assert w.tb.source.get_gain('RF') == 0
assert w.tb.source.get_gain('IF') == 16
assert w.tb.source.get_gain('BB') == 20
class FailedTx(s.QtCore.QThread):
    result = s.QtCore.pyqtSignal(object)
    def __init__(self, params, parent=None): super().__init__(parent)
    def run(self): self.result.emit(dict(ok=False, status='simulated hardware failure'))
    def cancel(self): self.requestInterruption()
live_tx = '--transmit-once' in sys.argv
if not live_tx:
    s.Transmitter = FailedTx
actual_root = s.core.ROOT
test_output = tempfile.TemporaryDirectory()
s.core.ROOT = Path(test_output.name)
w.send_packet()
deadline = time.monotonic()+15
while (w.tx_preparer is not None or w.tx_worker is not None) and time.monotonic() < deadline:
    pump(.02)
s.core.ROOT = actual_root
test_output.cleanup()
assert w.tx_worker is None and w.tb is not None, w.state.text()
assert w.tx_preparer is None
assert w.tb is original_tb and w.tb.waterfall is original_waterfall
assert w.plots == original_plots
if live_tx:
    assert w.last_tx_result['ok'], w.last_tx_result
pump(.5)
waterfall_samples_after = w.tb.waterfall.nitems_read(0)
assert waterfall_samples_after > 0
waterfall_samples_checkpoint = waterfall_samples_after
pump(.3)
assert w.tb.waterfall.nitems_read(0) > waterfall_samples_checkpoint, 'Waterfall stopped updating after TX'
w.grab().save(str(s.core.ROOT/'station-live-preview.png'))
report = dict(test=('HackRF RX/TX/RX, one low-gain CQ sent' if live_tx else
                    'HackRF live RX and recovery after simulated TX failure; no RF transmitted'),
              device=device, resumed=True, waterfall_preserved=True,
              rx_pause_s=w.last_rx_pause_s, tx_result=w.last_tx_result,
              waterfall_updating=True, waterfall_samples_after=w.tb.waterfall.nitems_read(0),
              scan_bandwidth_hz=int(w.entries['scan_bandwidth'].text()),
              packets=len(w.records), status=w.state.text())
w.stop(); w.close()
(s.core.ROOT/('station-tx-switch-test.json' if live_tx else 'station-live-test.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
