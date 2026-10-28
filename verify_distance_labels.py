"""Stress actual label bounds, antenna clearance, font changes and scroll anchoring."""
from pathlib import Path
import tempfile
import time

import receiver as rx


app = rx.QtWidgets.QApplication([])
app.setFont(rx.QtGui.QFont('Segoe UI', 9))


def settle():
    for _ in range(8):
        app.processEvents()


def verify_bounds(view):
    labels, _ = view.device_label_layout()
    axis_y = view.height()-(115 if view.controls.parent() is view else 80)
    antenna = rx.QtCore.QRectF(40, axis_y-110, 110, 110)
    for label in labels:
        rect = label['rect']
        assert rect.top() >= 44, (view.size(), rect)
        assert rect.left() >= 6 and rect.right() <= view.width()-6
        assert rect.bottom() <= axis_y-20
        assert rect.left() <= label['x'] <= rect.right()  # Straight-up leader meets its own label.
        assert not antenna.intersects(rect)
        assert all(view.fontMetrics().horizontalAdvance(line) <= rect.width() for line in label['lines']), (
            view.font().pointSize(), view.width(), rect.width(),
            [(line, view.fontMetrics().horizontalAdvance(line)) for line in label['lines']])
    assert all(not a['rect'].intersects(b['rect']) for i,a in enumerate(labels) for b in labels[i+1:])
    assert len(labels) == len(view.devices)


view = rx.DistanceView()
strengths = [5, 0, -0.01, -0.1, -5, -20, -30, -30, -30.01, -59.99, -60, -80]
for i, strength in enumerate(strengths):
    view.update_device(dict(fields=dict(device_id=i+1, name='LONG DEVICE NAME '+str(i)*20),
                            signal_avg_dbfs=strength))
for invalid in (float('nan'), float('inf'), -float('inf')):
    view.update_device(dict(fields=dict(device_id=99, name='invalid'), signal_avg_dbfs=invalid))
assert 99 not in view.devices
scenarios = 0
for font_size in (9, 16, 28):
    view.setFont(rx.QtGui.QFont('Segoe UI', font_size))
    for width in (360, 760, 1200):
        for mode in (0, 1):
            view.axis_mode.setCurrentIndex(mode)
            view.resize(max(width, view.minimumWidth()), view.minimumHeight())
            view.refresh_label_layout()
            view.resize(max(width, view.minimumWidth()), view.minimumHeight())
            verify_bounds(view)
            scenarios += 1
view.close()

with tempfile.TemporaryDirectory() as directory:
    preferences = rx.QtCore.QSettings(str(Path(directory)/'settings.ini'), rx.QtCore.QSettings.IniFormat)
    original_discovery = rx.discover_rtl_devices
    rx.discover_rtl_devices = lambda: []
    try:
        window = rx.Window(preferences)
    finally:
        rx.discover_rtl_devices = original_discovery
    window.show()
    settle()
    view = window.distance_window
    scrollbar = window.distance_scroll.verticalScrollBar()
    for i in range(12):
        view.update_device(dict(fields=dict(device_id=i+1, name=f'DENSE {i+1}'), signal_avg_dbfs=-30))
    settle()
    assert scrollbar.maximum() > 0 and scrollbar.value() == scrollbar.maximum()
    verify_bounds(view)
    scrollbar.setValue(0)  # Manual scrolling is preserved when another lane arrives.
    view.update_device(dict(fields=dict(device_id=13, name='NEW'), signal_avg_dbfs=-30))
    settle()
    assert scrollbar.value() == 0
    scrollbar.setValue(scrollbar.maximum())
    view.update_device(dict(fields=dict(device_id=14, name='NEW'), signal_avg_dbfs=-30))
    settle()
    assert scrollbar.value() == scrollbar.maximum()
    # Changing an existing device must not duplicate it, including after timeout.
    view.devices[1]['last_seen'] = time.monotonic()-100
    view.update_device(dict(fields=dict(device_id=1, name='RENAMED'), signal_avg_dbfs=-60))
    assert len(view.devices) == 14 and view.devices[1]['name'] == 'RENAMED'
    window.set_language('zh')
    window.resize(1100, 800)
    settle()
    verify_bounds(view)
    assert window.distance_window.controls.isVisible()
    # A representative end-point and close-signal preview, using synthetic devices only.
    view.devices.clear()
    for i, strength in enumerate((0, -29, -29.1, -60)):
        view.update_device(dict(fields=dict(device_id=0x999990+i, name=f'DEMO {i+1}'), signal_avg_dbfs=strength))
    view.axis_mode.setCurrentIndex(0)
    settle()
    window.grab().save(str(rx.ROOT/'distance-label-preview.png'))
    window.close()
print(f'PASS: {scenarios} dense/edge/font/axis label scenarios; scrolling and live updates')
