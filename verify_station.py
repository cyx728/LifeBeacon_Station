"""Synthetic TX/RX verification and GUI lifecycle. Does not radiate RF."""
import json
import tempfile
from pathlib import Path
import numpy as np
import station as s
from gnuradio import blocks

app = s.QtWidgets.QApplication([])
raw = s.cq_packet(0x59d8cf87)
fields = s.core.parse_lb(raw)
assert len(raw) == 10 and fields['device_id'] == 0x59d8cf87 and fields['message_type'] == 0
assert len(s.core.lb_field_details(raw, fields)) == 5
for i in range(10):
    damaged = bytearray(raw); damaged[i] ^= 1
    try:
        s.core.parse_lb(damaged)
    except ValueError:
        pass
    else:
        raise AssertionError('Corrupt packet accepted')
records = []
iq = s.generate_iq(raw)
p = dict(frequency=433375000, sample_rate=2000000, bw=125000, sf=7, sync=0x12)
graph = s.core.Receiver(p, records.append, False, blocks.vector_source_c(iq.tolist(), False))
graph.run()
valid = [r for r in records if r.get('raw_payload_hex', '').replace(' ', '').lower() == raw.hex()
         and r['status'] == 'PHY CRC OK / LB OK']
assert valid, records
window = s.Station()
app.setFont(s.QtGui.QFont('Segoe UI', 9))
window.show()
devices = s.discover_devices()
hackrf = next((d for d, name in devices if d.startswith('hackrf=')), None)
if hackrf:
    window.device_combo.setCurrentIndex(window.device_combo.findData(hackrf))
    app.processEvents()
    assert window.tx_panel.isVisible()
for lang in ('zh', 'en'):
    window.set_language(lang)
# Exercise asynchronous transmit failure without opening a radio sink.
class FailedTx(s.QtCore.QThread):
    result = s.QtCore.pyqtSignal(object)
    def __init__(self, params, parent=None): super().__init__(parent)
    def run(self): self.result.emit(dict(ok=False, status='simulated hardware failure'))
    def cancel(self): self.requestInterruption()
original_tx = s.Transmitter
s.Transmitter = FailedTx
if hackrf:
    actual_root = s.core.ROOT
    test_output = tempfile.TemporaryDirectory()
    s.core.ROOT = Path(test_output.name)
    window.send_packet()
    deadline = s.time.monotonic()+5
    while (window.tx_worker is not None or window.tx_preparer is not None) and s.time.monotonic() < deadline:
        app.processEvents()
        s.time.sleep(.01)
    assert window.tx_worker is None and window.tx_preparer is None and window.send_button.isEnabled()
    assert window.tx_status.text() == 'simulated hardware failure'
    s.core.ROOT = actual_root
    test_output.cleanup()
s.Transmitter = original_tx
window.grab().save(str(s.core.ROOT/'station-preview.png'))
window.close()
report = dict(test='synthetic IQ loopback, no RF transmitted', raw=raw.hex(' ').upper(),
              decoded=fields, matching_packets=len(valid), devices=devices)
(s.core.ROOT/'station-test-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
