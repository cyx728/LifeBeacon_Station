"""Check cross-channel suppression before GUI, visualization and JSONL writes."""
import json
import tempfile
from pathlib import Path

import receiver as rx

app = rx.QtWidgets.QApplication([])
window = rx.Window()
window.display_config['show_failed'] = True
window.apply_display_config()
with tempfile.TemporaryDirectory() as directory:
    window.log = Path(directory)/'received.jsonl'
    base = json.loads((rx.ROOT/'self-test-result.json').read_text())['records'][0]
    merger = window.packet_merger
    for frequency, strength in [(433712500, -42), (433775000, -19), (433837500, -35)]:
        merger.receive(dict(base, channel_frequency_hz=frequency, signal_avg_dbfs=strength))
    assert not window.records and not window.log.exists()
    merger.flush(now=max(g['deadline'] for g in merger.pending)+0.01)
    assert len(window.records) == 1
    assert window.records[0]['channel_frequency_hz'] == 433775000
    assert window.records[0]['signal_avg_dbfs'] == -19
    assert window.table.rowCount() == 1
    assert len(window.log.read_text().splitlines()) == 2
    # An identical later transmission must remain a distinct record.
    merger.receive(dict(base, channel_frequency_hz=433775000, signal_avg_dbfs=-22))
    merger.receive(dict(base, channel_frequency_hz=433837500, signal_avg_dbfs=-38))
    merger.flush(force=True)
    assert len(window.records) == 2
    assert len(window.log.read_text().splitlines()) == 3
    # Two receptions on one channel are never collapsed, even inside the window.
    for strength in [-25, -26]:
        merger.receive(dict(base, channel_frequency_hz=433775000, signal_avg_dbfs=strength))
    merger.flush(force=True)
    assert len(window.records) == 4
    # Different bytes remain distinct regardless of frequency or signal strength.
    merger.receive(dict(base, channel_frequency_hz=433775000, signal_avg_dbfs=-20))
    merger.receive(dict(base, raw_payload_hex='01 02', fields=None, status='PHY CRC FAIL',
                        phy_crc_valid=False,
                        channel_frequency_hz=433837500, signal_avg_dbfs=-10))
    merger.flush(force=True)
    assert len(window.records) == 5
    assert len(window.log.read_text().splitlines()) == 6
    window.on_packet(dict(base, status='PHY CRC FAIL', phy_crc_valid=False))
    merger.receive(dict(base, status='PHY CRC unavailable', phy_crc_valid=False))
    assert not merger.pending and len(window.records) == 5
    merger.receive(dict(base, raw_payload_hex='01 02', fields=None,
                        status='PHY CRC OK / LB REJECT: LB magic/length',
                        channel_frequency_hz=433837500, signal_avg_dbfs=-10))
    merger.flush(force=True)
    assert len(window.records) == 6
    assert len(window.log.read_text().splitlines()) == 7
    window.display_config['show_failed'] = False
    window.apply_display_config()
    assert window.table.rowCount() == 5
    assert len(window.log.read_text().splitlines()) == 7
    window.display_config['show_failed'] = True
    window.apply_display_config()
    assert window.table.rowCount() == 6
    window.close()
print('PASS: strongest channel only; no suppressed GUI/log entries; repeat/different packets preserved')
