"""RTL-SDR / gr-lora_sdr receiver. No transmit hardware is used."""
import argparse
import binascii
import csv
import ctypes
import ctypes.util
from datetime import datetime
import hashlib
import json
import math
import shutil
import threading
from collections import deque
from pathlib import Path
import sys
import time
import uuid

import numpy as np
import pmt
from PyQt5 import QtCore, QtGui, QtWidgets
import sip
from gnuradio import blocks, filter, gr, lora_sdr, qtgui
import osmosdr
from log_browser import LogBrowserDialog

ROOT = Path(__file__).resolve().parent
DECODE_COLUMNS = [
    ('device_id', 'Device ID'), ('name', 'Name'), ('emergency', 'Emergency level'),
    ('hr', 'Heart rate (bpm)'), ('spo2', 'SpO2 (%)'), ('battery', 'Battery (%)'),
    ('sequence', 'Sequence'), ('physiology_flags', 'Physiology flags'),
    ('physiology_reserved', 'Physiology reserved'), ('location_flag', 'Location flag'),
    ('rssi_placeholder', 'RSSI placeholder'), ('reserved', 'Reserved'),
    ('magic', 'Magic'), ('version', 'Version'), ('message_type', 'Message type'),
    ('name_length', 'Name length (bytes)'), ('hr_raw', 'Heart rate raw byte'),
    ('spo2_raw', 'SpO2 raw byte'), ('crc16_ccitt_le', 'LB CRC16 (little endian)'),
]
DECODE_LABELS_ZH = dict(zip([key for key, _ in DECODE_COLUMNS], [
    '设备 ID', '姓名', '紧急等级', '心率 (bpm)', '血氧 (%)', '电量 (%)', '序号',
    '生理标志', '生理保留字段', '定位标志', 'RSSI 占位值', '保留字段',
    '报文标识', '协议版本', '报文类型', '姓名字节数', '心率原始字节', '血氧原始字节', 'LB CRC16（小端）']))
DISPLAY_KEYS = ['time', 'raw_payload_hex'] + [key for key, _ in DECODE_COLUMNS] + [
    'status', 'signal_avg_dbfs', 'signal_peak_dbfs', 'channel_frequency_hz']
SCAN_CHANNEL_SPACING_HZ = 25000
# Keep adjacent 25 kHz-grid branches sufficiently isolated. The complete
# 125 kHz signal still fits because the grid error is at most 12.5 kHz.
CHANNEL_FILTER_CUTOFF_RATIO = 0.60
CHANNEL_FILTER_TRANSITION_RATIO = 0.15
MESSAGE_TYPES = {0: ('广泛呼叫', 'CQ'), 1: ('信标', 'beacon'), 2: ('应答', 'response')}
# Curated clean macaron hues: no arbitrary RGB mixtures or gray/brown shades.
MACARON_PALETTE = (
    '#FFD1E3', '#FFCFC4', '#FFDEBC', '#FFF2B3',
    '#E3F5B8', '#C2F2D5', '#BCEFEA', '#C5E6FF',
    '#D4DCFF', '#E6D2FF', '#F4CFFF', '#FFD4F0',
)


def scan_offsets(center, span, lora_bw):
    """25 kHz absolute frequency grid; keep complete LoRa channels in band."""
    low = math.ceil((center - (span-lora_bw)/2)/SCAN_CHANNEL_SPACING_HZ)
    high = math.floor((center + (span-lora_bw)/2)/SCAN_CHANNEL_SPACING_HZ)
    return [channel*SCAN_CHANNEL_SPACING_HZ-center for channel in range(low, high+1)]


def phy_crc_passed(record):
    return record.get('phy_crc_valid', record.get('status', '').startswith('PHY CRC OK')) is True


def message_type_label(value, language='en'):
    return MESSAGE_TYPES.get(value, ('保留', 'Reserved'))[0 if language == 'zh' else 1]


def decoder_sync_word(params):
    """Return strict configured sync or wildcard recovery mode."""
    return [0, 0] if params.get('sync_auto') else [int(params['sync'])]


def display_value(record, key, language):
    fields = record.get('fields') or {}
    if key in ('signal_avg_dbfs', 'signal_peak_dbfs'):
        value = record.get(key)
        return '—' if value is None else f'{value:.2f}'
    if key == 'channel_frequency_hz':
        value = record.get(key)
        return '—' if value is None else f'{value:.0f}'
    if key in ('time', 'raw_payload_hex', 'status'):
        return str(record.get(key, '—'))
    value = fields.get(key)
    if value is None:
        return '—'
    if key == 'device_id':
        return f'0x{value:08X}'
    if key == 'emergency':
        labels = (['未知', '正常', '警告', '危急'] if language == 'zh' else
                  ['Unknown', 'Normal', 'Warning', 'Critical'])
        label = labels[value] if 0 <= value < len(labels) else ('保留' if language == 'zh' else 'Reserved')
        return f'{value} ({label})'
    if key == 'message_type':
        return f'{value} ({message_type_label(value, language)})'
    if key in ('physiology_flags', 'hr_raw', 'spo2_raw'):
        return f'0x{value:02X}'
    if key == 'crc16_ccitt_le':
        return f'0x{value:04X}'
    return str(value)


def device_background(device_id):
    """Stable ID-derived macaron color, shared by rows and device markers."""
    digest = hashlib.sha256((int(device_id) & 0xffffffff).to_bytes(4, 'little')).digest()
    return QtGui.QColor(MACARON_PALETTE[int.from_bytes(digest[:4], 'little') % len(MACARON_PALETTE)])


def discover_rtl_devices():
    """Enumerate RTL USB devices without starting a receiver flowgraph."""
    library = (str(Path(sys.prefix)/'Library'/'bin'/'rtlsdr.dll') if sys.platform == 'win32'
               else ctypes.util.find_library('rtlsdr'))
    if not library:
        raise RuntimeError('RTL-SDR enumeration library unavailable')
    rtl = ctypes.CDLL(library)
    rtl.rtlsdr_get_device_count.restype = ctypes.c_uint32
    rtl.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    rtl.rtlsdr_get_device_name.restype = ctypes.c_char_p
    devices = []
    for index in range(rtl.rtlsdr_get_device_count()):
        name = rtl.rtlsdr_get_device_name(index)
        devices.append(('rtl=' + str(index), name.decode('utf-8', errors='replace') if name else 'RTL-SDR'))
    return devices


class DistanceView(QtWidgets.QWidget):
    """Approximate relative-distance view; it is not a calibrated ranging system."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.devices = {}
        self.timeout_seconds = 15.0
        self.setMinimumSize(760, 420)
        self.setWindowTitle('Distance visualization')
        self.controls = QtWidgets.QWidget(self)
        controls_layout = QtWidgets.QHBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        self.timeout_label = QtWidgets.QLabel()
        controls_layout.addWidget(self.timeout_label)
        self.timeout_input = QtWidgets.QDoubleSpinBox()
        self.timeout_input.setRange(0.1, 86400)
        self.timeout_input.setDecimals(1)
        self.timeout_input.setValue(self.timeout_seconds)
        self.timeout_input.setFixedWidth(90)
        self.timeout_input.valueChanged.connect(self.set_timeout)
        controls_layout.addWidget(self.timeout_input)
        self.timeout_unit = QtWidgets.QLabel()
        controls_layout.addWidget(self.timeout_unit)
        controls_layout.addSpacing(24)
        self.axis_label = QtWidgets.QLabel()
        controls_layout.addWidget(self.axis_label)
        self.axis_mode = QtWidgets.QComboBox()
        self.axis_mode.addItem('', 'linear')
        self.axis_mode.addItem('', 'log_distance')
        self.axis_mode.currentIndexChanged.connect(self.refresh_label_layout)
        controls_layout.addWidget(self.axis_mode)
        controls_layout.addStretch()
        self.controls.setGeometry(20, self.height()-48, self.width()-40, 32)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(500)
        self.set_language('en')

    def set_language(self, language):
        self.language = 'zh' if language == 'zh' else 'en'
        chinese = self.language == 'zh'
        self.setWindowTitle('距离可视化' if chinese else 'Distance visualization')
        self.timeout_label.setText('超时：' if chinese else 'Timeout:')
        self.timeout_unit.setText('(秒)' if chinese else '(s)')
        self.axis_label.setText('坐标轴：' if chinese else 'Axis:')
        self.axis_mode.setItemText(0, '线性 (dBFS)' if chinese else 'Linear (dBFS)')
        self.axis_mode.setItemText(1, '对数传播 (n=2)' if chinese else 'Log-distance (n=2)')
        self.axis_mode.setToolTip(
            '自由空间模型：ΔP = −20 log10(d/d₀)。\n'
            '相对距离 ∝ 10^(−P/20)，端点为 0 和 −60 dBFS。\n'
            '假设发射功率相同、接收增益固定；未进行米制距离标定。' if chinese else
            'Free-space model: ΔP = −20 log10(d/d₀).\n'
            'Relative distance ∝ 10^(−P/20); endpoints remain 0 and −60 dBFS.\n'
            'Assumes equal transmitter power and fixed receiver gain; no meter calibration.')
        self.antenna_text = '天线' if chinese else 'ANTENNA'
        self.heading_text = ('设备信号强度 (dBFS)' if chinese else 'Device signal strength (dBFS)')
        self.refresh_label_layout()
        self.update()

    def set_timeout(self, seconds):
        self.timeout_seconds = seconds
        self.update()

    def axis_position(self, strength):
        attenuation = max(0.0, min(60.0, -strength))
        if self.axis_mode.currentData() == 'log_distance':
            # Free-space path loss has n=2: received power falls as 1/d².
            return math.expm1(math.log(10)*attenuation/20) / 999.0
        return attenuation / 60.0

    def resizeEvent(self, event):
        if self.controls.parent() is self:
            self.controls.setGeometry(20, self.height()-48, self.width()-40, 32)
        self.refresh_label_layout()
        super().resizeEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.ApplicationFontChange,
                            QtCore.QEvent.StyleChange) and hasattr(self, 'axis_mode'):
            self.refresh_label_layout()

    def label_minimum_width(self):
        metrics = self.fontMetrics()
        return max(360, max((metrics.horizontalAdvance(f"0x{d['id']:08X}")+24
                            for d in self.devices.values()), default=0),
                   max((metrics.horizontalAdvance(f"{d['strength']:.1f} dBFS")+24
                        for d in self.devices.values()), default=0))

    def device_label_layout(self):
        """Pack horizontal label intervals into non-overlapping vertical lanes."""
        metrics = self.fontMetrics()
        line_height = metrics.lineSpacing()
        width = self.width()
        left, right = 95, max(135, width-45)
        label_left = 6
        preferred_width = max(260, max((metrics.horizontalAdvance(f"0x{d['id']:08X}")+12
                                       for d in self.devices.values()), default=0),
                              max((metrics.horizontalAdvance(f"{d['strength']:.1f} dBFS")+12
                                   for d in self.devices.values()), default=0))
        max_width = max(100, min(preferred_width, width-label_left-12))
        labels, lane_ends = [], []
        devices = sorted(self.devices.values(), key=lambda d: (self.axis_position(d['strength']), d['id']))
        for device in devices:
            x = left + (right-left)*self.axis_position(device['strength'])
            lines = [f"0x{device['id']:08X}",
                     metrics.elidedText(device['name'] or '—', QtCore.Qt.ElideRight, max_width-12),
                     f"{device['strength']:.1f} dBFS"]
            label_width = min(max_width, max(metrics.horizontalAdvance(line) for line in lines)+12)
            label_x = max(label_left, min(x-label_width/2, width-label_width-6))
            # Lift labels above the antenna when their text spans its icon.
            first_lane = (math.ceil(100/(3*line_height+12))
                          if label_x < left+55 and label_x+label_width > left-55 else 0)
            lane = next((i for i, end in enumerate(lane_ends)
                         if i >= first_lane and label_x >= end+12), max(first_lane, len(lane_ends)))
            while lane >= len(lane_ends):
                lane_ends.append(-math.inf)
            lane_ends[lane] = label_x+label_width
            bottom_padding = 115 if self.controls.parent() is self else 80
            top = self.height()-bottom_padding-20-3*line_height-lane*(3*line_height+12)
            labels.append(dict(device=device, x=x, lines=lines, lane=lane,
                               rect=QtCore.QRectF(label_x, top, label_width, 3*line_height)))
        return labels, len(lane_ends)

    def refresh_label_layout(self):
        minimum_width = self.label_minimum_width()
        if self.minimumWidth() != minimum_width:
            self.setMinimumWidth(minimum_width)
        _, lanes = self.device_label_layout()
        # The surrounding scroll area accommodates dense clusters without clipping.
        bottom_padding = 135 if self.controls.parent() is self else 100
        required = max(245, 55 + lanes*(3*self.fontMetrics().lineSpacing()+12) + bottom_padding)
        if self.minimumHeight() != required:
            self.setMinimumHeight(required)
        self.update()

    def update_device(self, record):
        fields = record.get('fields') or {}
        device_id = fields.get('device_id')
        strength = record.get('signal_avg_dbfs')
        if device_id is None or strength is None:
            return
        now = record.get('_received_monotonic', time.monotonic())
        strength = float(strength)
        if not math.isfinite(strength):
            return
        self.devices[int(device_id)] = {
            'id': int(device_id), 'name': fields.get('name') or '',
            'strength': float(strength), 'last_seen': now,
            'status': record.get('status', '')}
        self.refresh_label_layout()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor('#ffffff'))
        width, height = self.width(), self.height()
        axis_y = height - (115 if self.controls.parent() is self else 80)
        left = 95
        right = max(left + 40, width - 45)
        painter.setPen(QtGui.QPen(QtGui.QColor('#303030'), 3))
        painter.drawLine(left, axis_y, right, axis_y)
        painter.setPen(QtGui.QPen(QtGui.QColor('#808080'), 1))
        last_label_right = -1
        for i in range(0, 7):
            x = left + (right - left) * self.axis_position(-i * 10)
            painter.drawLine(int(x), axis_y - 6, int(x), axis_y + 6)
            text = f'{-i * 10} dBFS'
            label_width = painter.fontMetrics().horizontalAdvance(text)
            if x - label_width/2 > last_label_right + 8:
                painter.drawText(int(x - label_width/2), int(axis_y + 28), text)
                last_label_right = x + label_width/2
        # Draw a simple antenna icon at the zero-distance origin.
        painter.setPen(QtGui.QPen(QtGui.QColor('#1e5aa8'), 4))
        painter.drawLine(left, axis_y, left, axis_y - 65)
        painter.drawLine(left - 18, axis_y - 45, left, axis_y - 65)
        painter.drawLine(left + 18, axis_y - 45, left, axis_y - 65)
        painter.setPen(QtGui.QPen(QtGui.QColor('#1e5aa8'), 2))
        painter.drawArc(left - 35, axis_y - 92, 70, 55, 20 * 16, 140 * 16)
        painter.drawArc(left - 52, axis_y - 110, 104, 85, 20 * 16, 140 * 16)
        painter.setPen(QtGui.QColor('#202020'))
        painter.drawText(left - 30, int(axis_y + 55), self.antenna_text)
        painter.drawText(20, 28, self.heading_text)
        now = time.monotonic()
        # Each device is drawn directly on the right-side number axis. Its x
        # position is replaced in-place whenever a new frame for that ID arrives.
        labels, _ = self.device_label_layout()
        marker_radius = 8
        for label in labels:
            device = label['device']
            age = now - device['last_seen']
            x = label['x']
            y = axis_y
            alpha = 255 if age <= self.timeout_seconds else 70
            rect = label['rect']
            painter.setPen(QtGui.QPen(QtGui.QColor(135, 145, 155, alpha), 1))
            # Draw connectors first so label backgrounds can mask crossings.
            painter.drawLine(QtCore.QPointF(x, y-marker_radius), QtCore.QPointF(x, rect.bottom()))
        for label in labels:
            device = label['device']
            alpha = 255 if now-device['last_seen'] <= self.timeout_seconds else 70
            x, y, rect = label['x'], axis_y, label['rect']
            color = device_background(device['id'])
            color.setAlpha(alpha)
            painter.setBrush(color)
            painter.setPen(QtGui.QPen(color, 1))
            painter.drawEllipse(QtCore.QPointF(x, y), marker_radius, marker_radius)
            painter.fillRect(rect, QtGui.QColor('#ffffff'))
            painter.setPen(QtGui.QColor(30, 30, 30, alpha))
            line_height = painter.fontMetrics().lineSpacing()
            for line_number, line in enumerate(label['lines']):
                line_rect = QtCore.QRectF(rect.left(), rect.top()+line_number*line_height,
                                         rect.width(), line_height)
                painter.drawText(line_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter, line)


def parse_lb(data):
    if len(data) == 10 and data[:3] == b'LB\x01' and data[7] == 0:
        crc = binascii.crc_hqx(data[:-2], 0xffff)
        if crc != int.from_bytes(data[-2:], 'little'):
            raise ValueError('LB CRC16 mismatch')
        return dict(magic='LB', version=1, device_id=int.from_bytes(data[3:7], 'little'),
                    message_type=0, crc16_ccitt_le=crc, packet_format='short_cq')
    if len(data) < 21 or data[:2] != b'LB':
        raise ValueError('LB magic/length')
    n = data[9]
    if n > 31 or len(data) != 21 + n:
        raise ValueError('LB name/length')
    if data[2] != 1 or data[3] not in MESSAGE_TYPES:
        raise ValueError('LB version/type')
    crc = binascii.crc_hqx(data[:-2], 0xffff)
    if crc != int.from_bytes(data[-2:], 'little'):
        raise ValueError('LB CRC16 mismatch')
    i = 10 + n
    flags, hr, spo2, reserved = data[i:i+4]
    return dict(magic=data[:2].decode('ascii'), version=data[2], message_type=data[3], name_length=n,
                device_id=int.from_bytes(data[5:9], 'little'),
                name=data[10:i].decode('ascii', errors='replace'), emergency=data[4],
                hr=hr if flags & 1 and hr != 255 else None,
                spo2=spo2 if flags & 2 and spo2 != 255 else None,
                hr_raw=hr, spo2_raw=spo2, crc16_ccitt_le=int.from_bytes(data[-2:], 'little'),
                physiology_flags=flags, physiology_reserved=reserved, location_flag=data[i+4],
                battery=data[i+5], sequence=data[i+6],
                rssi_placeholder=data[i+7], reserved=data[i+8])


def lb_field_details(data, fields):
    """Exact TX-compatible fields and inclusive offsets for a validated LB packet."""
    if fields.get('packet_format') == 'short_cq':
        return [dict(field=key, key=key, offset_start=a, offset_end=b-1,
                     raw_hex=data[a:b].hex(' ').upper(), value=fields[key])
                for key, a, b in [('magic', 0, 2), ('version', 2, 3), ('device_id', 3, 7),
                                  ('message_type', 7, 8), ('crc16_ccitt_le', 8, 10)]]
    i = 10 + fields['name_length']
    specs = [
        ('magic', 'magic', 0, 2), ('version', 'version', 2, 3),
        ('message_type', 'message_type', 3, 4), ('emergency_level', 'emergency', 4, 5),
        ('device_id_le', 'device_id', 5, 9), ('name_length', 'name_length', 9, 10),
        ('name_ascii', 'name', 10, i), ('physiology_flags', 'physiology_flags', i, i+1),
        ('heart_rate_bpm', 'hr_raw', i+1, i+2), ('spo2_percent', 'spo2_raw', i+2, i+3),
        ('physiology_reserved', 'physiology_reserved', i+3, i+4),
        ('location_flag', 'location_flag', i+4, i+5), ('battery_percent', 'battery', i+5, i+6),
        ('sequence', 'sequence', i+6, i+7), ('rssi_placeholder', 'rssi_placeholder', i+7, i+8),
        ('reserved_1', 'reserved', i+8, i+9), ('crc16_ccitt_le', 'crc16_ccitt_le', i+9, i+11),
    ]
    return [dict(field=name, key=key, offset_start=start, offset_end=end-1,
                 raw_hex=data[start:end].hex(' ').upper(), value=fields[key])
            for name, key, start, end in specs]


def decode_record(record):
    """Derive fields from this record's bytes, never from earlier packets or cached fields."""
    result = dict(record)
    if not phy_crc_passed(record):
        return dict(result, fields=None, payload_fields=[], message_type_label=None)
    try:
        raw = bytes.fromhex(record['raw_payload_hex'])
        fields = parse_lb(raw)
    except (KeyError, TypeError, ValueError) as exc:
        result.update(fields=None, payload_fields=[], message_type_label=None,
                      status='PHY CRC OK / LB REJECT: ' + str(exc))
    else:
        result.update(fields=fields, payload_fields=lb_field_details(raw, fields),
                      message_type_label=message_type_label(fields['message_type']), status='PHY CRC OK / LB OK')
    return result


class Events(QtCore.QObject):
    packet = QtCore.pyqtSignal(object)


class ChannelPacketMerger(QtCore.QObject):
    """Hold live candidates before any display or disk write."""
    def __init__(self, publish, parent=None, window_seconds=0.75):
        super().__init__(parent)
        self.publish = publish
        self.window_seconds = window_seconds
        self.pending = []
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.flush)
        self.timer.start()

    @staticmethod
    def strength(record):
        try:
            value = float(record.get('signal_avg_dbfs'))
            return value if math.isfinite(value) else -math.inf
        except (ValueError, TypeError):
            return -math.inf

    def receive(self, record):
        if not phy_crc_passed(record):
            return
        now = time.monotonic()
        self.flush(now=now)
        record = dict(record, _received_monotonic=now)
        try:
            payload = bytes.fromhex(record['raw_payload_hex'])
        except (KeyError, ValueError, TypeError):
            self.publish(record)
            return
        frequency = record.get('channel_frequency_hz')
        for group in self.pending:
            if group['payload'] != payload:
                continue
            # Same-channel repeats are separate receptions, never globally
            # deduplicate recurring telemetry solely by payload content.
            if frequency is not None and frequency not in group['channels']:
                group['channels'].add(frequency)
                if self.strength(record) > self.strength(group['best']):
                    group['best'] = record
                return
        self.pending.append(dict(payload=payload, channels={frequency}, best=record,
                                 deadline=now+self.window_seconds))

    def flush(self, force=False, now=None):
        now = time.monotonic() if now is None else now
        ready = [group for group in self.pending if force or now >= group['deadline']]
        self.pending = [group for group in self.pending if not (force or now >= group['deadline'])]
        for group in ready:
            self.publish(group['best'])


class FramePower(gr.sync_block):
    """Measure synchronized header/payload samples; ignore rejected headers."""
    def __init__(self):
        gr.sync_block.__init__(self, 'per-frame IQ power', [np.complex64], [np.complex64])
        self.frames = deque()
        self.lock = threading.Lock()
        self.current = None

    def work(self, inputs, outputs):
        data = inputs[0]
        outputs[0][:len(data)] = data
        start = self.nitems_read(0)
        tags = sorted(self.get_tags_in_window(0, 0, len(data), pmt.intern('frame_info')),
                      key=lambda tag: tag.offset)
        pos = 0
        for tag in tags:
            end = int(tag.offset - start)
            self.accumulate(data[pos:end])
            info = tag.value
            header = pmt.to_bool(pmt.dict_ref(info, pmt.intern('is_header'), pmt.PMT_F))
            if header:
                sf = pmt.to_long(pmt.dict_ref(info, pmt.intern('sf'), pmt.from_long(7)))
                self.current = dict(count=0, total=0.0, peak=0.0, expected=None, bins=2**sf)
            elif self.current is not None:
                symbols = pmt.to_long(pmt.dict_ref(info, pmt.intern('symb_numb'), pmt.from_long(0)))
                self.current['expected'] = symbols * self.current['bins']
                with self.lock:
                    self.frames.append(self.current)
            pos = end
        self.accumulate(data[pos:])
        return len(data)

    def accumulate(self, samples):
        if self.current is None or not len(samples):
            return
        frame = self.current
        if frame['expected'] is not None:
            samples = samples[:max(0, frame['expected'] - frame['count'])]
        if len(samples):
            power = np.abs(samples.astype(np.complex128))**2
            with self.lock:
                frame['count'] += len(power)
                frame['total'] += float(power.sum())
                frame['peak'] = max(frame['peak'], float(power.max()))

    def take(self):
        with self.lock:
            if not self.frames:
                return {}
            frame = self.frames.popleft()
            # A missing/incomplete measurement must not be attributed to a packet.
            if not frame['count'] or frame['count'] != frame['expected']:
                return {}
            return dict(signal_avg_dbfs=10*math.log10(max(frame['total']/frame['count'], 1e-30)),
                        signal_peak_dbfs=10*math.log10(max(frame['peak'], 1e-30)),
                        signal_samples=frame['count'],
                        signal_scope='synchronized header and payload IQ, before FFT')


class PacketSink(gr.sync_block):
    """Read binary bytes and frame_info tags, never PMT text conversion."""
    def __init__(self, callback, power=None):
        gr.sync_block.__init__(self, 'raw payload and CRC status', [np.uint8], [])
        self.callback = callback
        self.power = power
        self.pending = None

    def work(self, inputs, outputs):
        data = inputs[0]
        start = self.nitems_read(0)
        tags = self.get_tags_in_window(0, 0, len(data), pmt.intern('frame_info'))
        indexed = {t.offset - start: t.value for t in tags}
        for pos, byte in enumerate(data):
            if pos in indexed:
                info = indexed[pos]
                length = pmt.to_long(pmt.dict_ref(info, pmt.intern('pay_len'), pmt.from_long(0)))
                valid = pmt.dict_ref(info, pmt.intern('crc_valid'), pmt.PMT_NIL)
                self.pending = [length, None if pmt.is_null(valid) else pmt.to_bool(valid), bytearray()]
            if self.pending:
                self.pending[2].append(int(byte))
                if len(self.pending[2]) == self.pending[0]:
                    length, phy, raw = self.pending
                    self.pending = None
                    status = 'PHY CRC OK' if phy is True else ('PHY CRC FAIL' if phy is False else 'PHY CRC unavailable')
                    record = dict(time=time.strftime('%Y-%m-%d %H:%M:%S'),
                                  raw_payload_hex=raw.hex(' ').upper(), status=status, fields=None,
                                  phy_crc_valid=phy is True)
                    if self.power is not None:
                        record.update(self.power.take())
                    # Always consume this frame's power result, including on
                    # rejection, so subsequent packets keep their measurements.
                    if phy is True:
                        self.callback(decode_record(record))
        return len(data)


class Receiver(gr.top_block):
    def __init__(self, params, callback, spectrum=True, source=None):
        gr.top_block.__init__(self, 'LifeBeacon RTL-SDR LoRa', catch_exceptions=True)
        self.source_params = dict(params)
        self.rx = lora_sdr.lora_sdr_lora_rx(center_freq=params['frequency'], bw=params['bw'],
            sf=params['sf'], cr=1, has_crc=True, impl_head=False, pay_len=255,
            samp_rate=params['bw']*4, sync_word=decoder_sync_word(params), soft_decoding=True,
            ldro_mode=2, print_rx=[False, False])
        self.power = FramePower()
        self.rx.disconnect(self.rx.lora_sdr_frame_sync_0, self.rx.lora_sdr_fft_demod_0)
        self.rx.connect(self.rx.lora_sdr_frame_sync_0, self.power, self.rx.lora_sdr_fft_demod_0)
        self.sink = PacketSink(callback, self.power)
        self.connect(self.rx, self.sink)
        if source is None:
            self.source = osmosdr.source(args='numchan=1 ' + params.get('device', 'rtl=0'))
            self.source.set_sample_rate(params['sample_rate'])
            self.source.set_center_freq(params['frequency'])
            self.source.set_freq_corr(params['ppm'])
            self.source.set_gain_mode(False)
            if params.get('device', '').startswith('hackrf='):
                self.source.set_gain(params.get('hackrf_lna', 16), 'IF')
                self.source.set_gain(params.get('hackrf_vga', 20), 'BB')
                self.source.set_gain(14 if params.get('hackrf_amp') else 0, 'RF')
            else:
                self.source.set_gain(params['gain'])
            self.source.set_bandwidth(0)
            actual = self.source.get_sample_rate()
            if abs(actual - params['sample_rate']) > 1:
                raise ValueError('RTL-SDR sample rate differs from requested rate: ' + str(actual))
        else:
            self.source = source
        rate = int(params['sample_rate'])
        target = int(params['bw']*4)
        common = math.gcd(rate, target)
        self.resampler = filter.rational_resampler_ccf(target//common, rate//common, [], 0.4)
        self.connect(self.source, self.resampler, self.rx)
        self.channels = []
        if params.get('scan_bandwidth'):
            self.disconnect(self.resampler, self.rx)
            self.disconnect(self.rx, self.sink)
            self.disconnect(self.source, self.resampler)
            span = int(params['scan_bandwidth'])
            bw = params['bw']
            offsets = scan_offsets(params['frequency'], span, bw)
            if not offsets:
                raise ValueError('No complete 25 kHz-grid LoRa channel fits in scan band')
            decimation = max(1, rate//target)
            for offset in sorted(set(offsets)):
                translator = filter.freq_xlating_fir_filter_ccc(decimation,
                    filter.firdes.low_pass(1, rate, bw*CHANNEL_FILTER_CUTOFF_RATIO,
                                           bw*CHANNEL_FILTER_TRANSITION_RATIO), offset, rate)
                # Rational ratio uses exact input rate, including non-divisible Fs.
                branch_common = math.gcd(target*decimation, rate)
                resampler = filter.rational_resampler_ccf(target*decimation//branch_common,
                                                          rate//branch_common, [], 0.4)
                resampler.set_min_output_buffer(65536)
                self.connect(self.source, translator, resampler)
                for sf in range(7, 13):
                    rx = lora_sdr.lora_sdr_lora_rx(center_freq=params['frequency']+offset, bw=bw,
                        sf=sf, cr=1, has_crc=True, impl_head=False, pay_len=255,
                        samp_rate=target, sync_word=decoder_sync_word(params), soft_decoding=True,
                        ldro_mode=2, print_rx=[False, False])
                    power = FramePower()
                    rx.disconnect(rx.lora_sdr_frame_sync_0, rx.lora_sdr_fft_demod_0)
                    rx.connect(rx.lora_sdr_frame_sync_0, power, rx.lora_sdr_fft_demod_0)
                    def received(record, freq=params['frequency']+offset, spreading=sf):
                        record.update(channel_frequency_hz=freq, spreading_factor=spreading,
                                      lora_bandwidth_hz=bw)
                        callback(record)
                    sink = PacketSink(received, power)
                    self.connect(resampler, rx, sink)
                    self.channels.append((translator, resampler, rx, power, sink))
        self.spectrum = None
        self.waterfall = None
        if spectrum:
            plot_rate = rate if params.get('scan_bandwidth') else target
            plot_source = self.source if params.get('scan_bandwidth') else self.resampler
            self.spectrum = qtgui.freq_sink_c(1024, 1, 0, plot_rate, '', 1)
            self.spectrum.set_update_time(0.15)
            self.spectrum.set_y_axis(-120, 0)
            self.spectrum.disable_legend()
            self.spectrum.enable_axis_labels(False)
            self.connect(plot_source, self.spectrum)
            self.waterfall = qtgui.waterfall_sink_c(1024, 1, 0, plot_rate, '', 1)
            self.waterfall.set_update_time(0.15)
            self.waterfall.set_intensity_range(-120, 0)
            self.waterfall.enable_axis_labels(False)
            self.connect(plot_source, self.waterfall)

    def set_gain(self, gain):
        """Apply manual RTL gain while the flowgraph is running."""
        if hasattr(self.source, 'set_gain'):
            self.source.set_gain(float(gain))

    def release_radio(self):
        """Release only USB hardware; retain decoders and spectrum/waterfall objects."""
        self.stop()
        self.wait()
        if self.source_params.get('scan_bandwidth'):
            targets = list(dict.fromkeys(channel[0] for channel in self.channels))
            targets += [s for s in (self.spectrum, self.waterfall) if s is not None]
        else:
            targets = [self.resampler]
        for target in targets:
            self.disconnect(self.source, target)
        self.radio_targets = targets
        self.source = None

    def restore_radio(self):
        p = self.source_params
        self.source = osmosdr.source(args='numchan=1 '+p['device'])
        self.source.set_sample_rate(p['sample_rate'])
        self.source.set_center_freq(p['frequency'])
        self.source.set_freq_corr(p['ppm'])
        self.source.set_gain_mode(False)
        self.source.set_bandwidth(0)
        if p.get('device', '').startswith('hackrf='):
            self.source.set_gain(p.get('hackrf_lna', 16), 'IF')
            self.source.set_gain(p.get('hackrf_vga', 20), 'BB')
            self.source.set_gain(14 if p.get('hackrf_amp') else 0, 'RF')
        else:
            self.source.set_gain(p.get('gain', 28))
        for target in self.radio_targets:
            self.connect(self.source, target)
        self.start()


class Window(QtWidgets.QWidget):
    def __init__(self, preferences=None):
        super().__init__()
        self.setWindowTitle('GNU Radio — RTL-SDR LoRa / LifeBeacon')
        self.resize(1600, 940)
        self.tb = None
        self.records = []
        self.config_history = []
        self.active_config = None
        self.active_log_path = None
        self.active_params = None
        self.plots = []
        self.distance_window = None
        self.preferences = preferences if preferences is not None else QtCore.QSettings('LifeBeacon', 'LoRaReceiver')
        self.device = self.preferences.value('receiver_device', 'rtl=0')
        self.device_names = {}
        self.discovery_error = ''
        self.display_config = dict(order=DISPLAY_KEYS[:], hidden=[], bold=[], color='alternate', show_failed=True)
        try:
            saved = json.loads(self.preferences.value('display_config', '{}'))
            self.display_config.update({key: saved[key] for key in self.display_config if key in saved})
        except (ValueError, TypeError):
            pass
        self.display_config['order'] = list(dict.fromkeys(
            [key for key in self.display_config['order'] if key in DISPLAY_KEYS] + DISPLAY_KEYS))
        self.language = 'en'
        self.translations = {
            'en': {'device': 'Device', 'logs': 'Logs', 'help': 'Help', 'configure': 'Configure device…',
                   'start': 'Start reception', 'stop': 'Stop reception', 'exit': 'Exit',
                   'language': 'Language', 'english': 'English', 'chinese': '中文',
                   'config': 'Receiver configuration',
                   'start_button': 'Start reception', 'stop_button': 'Stop',
                   'decoded': 'Decoded packets', 'frequency': 'Center frequency (Hz)',
                   'scan_bandwidth': 'Scan bandwidth (Hz)',
                   'sample_rate': 'RTL sample rate (S/s)', 'bw': 'LoRa bandwidth (Hz)',
                   'sf': 'SF scan', 'sync': 'Sync word (hex)', 'gain': 'Receiver gain',
                   'ppm': 'Frequency correction (ppm)',
                   'spectrum': 'Spectrum — frequency offset from tuned center',
                   'waterfall': 'Waterfall — frequency offset from tuned center'},
            'zh': {'device': '设备', 'logs': '日志', 'help': '帮助', 'configure': '配置设备…',
                   'start': '开始接收', 'stop': '停止接收', 'exit': '退出',
                   'language': '语言', 'english': 'English', 'chinese': '中文',
                   'config': '接收配置',
                   'start_button': '开始接收', 'stop_button': '停止',
                   'decoded': '已解码报文', 'frequency': '中心频率 (Hz)',
                   'scan_bandwidth': '扫描带宽 (Hz)',
                   'sample_rate': 'RTL 采样率 (S/s)', 'bw': 'LoRa 带宽 (Hz)',
                   'sf': '扩频扫描', 'sync': '同步字 (十六进制)', 'gain': '接收增益',
                   'ppm': '频率校正 (ppm)',
                   'spectrum': '频谱 — 相对调谐中心的频率偏移',
                   'waterfall': '瀑布图 — 相对调谐中心的频率偏移'}}
        self.events = Events()
        self.packet_merger = ChannelPacketMerger(self.on_packet, self)
        self.events.packet.connect(self.packet_merger.receive)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 6)
        self.menu = QtWidgets.QMenuBar()
        layout.setMenuBar(self.menu)
        self.device_menu = self.menu.addMenu('Device')
        self.start_action = self.device_menu.addAction('Start reception', self.start)
        self.stop_action = self.device_menu.addAction('Stop reception', self.stop)
        self.stop_action.setEnabled(False)
        self.device_menu.addSeparator()
        self.configure_action = self.device_menu.addAction('Configure device…', self.configure_device)
        self.refresh_action = self.device_menu.addAction('Refresh devices', self.refresh_devices)
        self.device_menu.addSeparator()
        self.exit_action = self.device_menu.addAction('Exit', self.close)
        self.logs_menu = self.menu.addMenu('Logs')
        self.open_logs_action = self.logs_menu.addAction('Open historical logs…', self.open_historical_logs)
        self.statistics_action = self.logs_menu.addAction('Current session statistics…', self.show_session_statistics)
        self.logs_menu.addSeparator()
        self.edit_display_action = self.logs_menu.addAction('Edit live log display…', self.edit_live_display)
        self.logs_menu.addSeparator()
        self.export_menu = self.logs_menu.addMenu('Export current session')
        self.export_jsonl_action = self.export_menu.addAction('JSONL…', self.export_jsonl)
        self.export_csv_action = self.export_menu.addAction('CSV…', self.export_csv)
        self.history_action = self.logs_menu.addAction('Save an existing log file as…', self.save_existing_log)
        self.visual_menu = self.menu.addMenu('Visualization')
        self.visual_action = self.visual_menu.addAction('Distance visualization')
        self.visual_action.setCheckable(True)
        self.visual_action.setChecked(True)
        self.visual_action.toggled.connect(self.set_distance_visible)
        self.help_menu = self.menu.addMenu('Help')
        self.measurement_action = self.help_menu.addAction('Signal measurement details', self.show_measurement_help)
        self.language_menu = self.menu.addMenu('Language')
        self.language_group = QtWidgets.QActionGroup(self)
        self.english_action = self.language_menu.addAction('English', lambda: self.set_language('en'))
        self.chinese_action = self.language_menu.addAction('中文', lambda: self.set_language('zh'))
        for action in (self.english_action, self.chinese_action):
            action.setCheckable(True)
            self.language_group.addAction(action)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        layout.addWidget(self.splitter, 1)
        left = QtWidgets.QWidget()
        left.setMinimumWidth(360)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 6, 6, 0)
        config = QtWidgets.QGroupBox('Receiver configuration')
        self.config_group = config
        config_layout = QtWidgets.QVBoxLayout(config)
        self.devices_title = QtWidgets.QLabel()
        self.devices_title.setStyleSheet('font-weight: 600;')
        device_header = QtWidgets.QHBoxLayout()
        device_header.addWidget(self.devices_title)
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(12)
        self.device_combo.currentIndexChanged.connect(self.select_device)
        device_header.addWidget(self.device_combo, 1)
        self.refresh_button = QtWidgets.QToolButton()
        self.refresh_button.clicked.connect(self.refresh_devices)
        device_header.addWidget(self.refresh_button)
        config_layout.addLayout(device_header)
        self.device_note = QtWidgets.QLabel()
        self.device_note.setWordWrap(True)
        config_layout.addWidget(self.device_note)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.advanced_title = QtWidgets.QLabel()
        self.advanced_title.setStyleSheet('font-weight: 600;')
        self.advanced_panel = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(self.advanced_panel)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.entries = {}
        self.form_labels = {}
        for key, label, default in [('frequency', 'Frequency (Hz)', 433775000),
            ('scan_bandwidth', 'Scan bandwidth (Hz)', 1000000),
            ('sample_rate', 'RTL sample rate (S/s)', 2400000), ('bw', 'LoRa bandwidth (Hz)', 125000),
            ('sync', 'Sync word (hex)', '0x12'),
            ('ppm', 'Frequency correction (ppm)', 0)]:
            entry = QtWidgets.QLineEdit(str(default))
            self.entries[key] = entry
            self.form_labels[key] = QtWidgets.QLabel(label)
            target = form if key in ('frequency', 'scan_bandwidth') else advanced_form
            target.addRow(self.form_labels[key], entry)
        self.sync_verify = QtWidgets.QCheckBox()
        self.sync_verify.setChecked(True)
        advanced_form.addRow(self.sync_verify)
        self.entries['gain'] = QtWidgets.QLineEdit('28')
        self.entries['gain'].setFixedWidth(65)
        self.entries['gain'].setAlignment(QtCore.Qt.AlignRight)
        self.entries['gain'].setValidator(QtGui.QDoubleValidator(0, 50, 1, self))
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gain_slider.setRange(0, 50)
        self.gain_slider.setValue(28)
        self.gain_slider.valueChanged.connect(self._gain_slider_changed)
        gain_row = QtWidgets.QHBoxLayout()
        gain_row.addWidget(self.gain_slider, 1)
        gain_row.addWidget(self.entries['gain'])
        gain_row.addWidget(QtWidgets.QLabel('dB'))
        self.form_labels['gain'] = QtWidgets.QLabel()
        form.addRow(self.form_labels['gain'], gain_row)
        self.entries['gain'].editingFinished.connect(self.apply_gain_entry)
        config_layout.addLayout(form)
        config_layout.addWidget(self.advanced_title)
        config_layout.addWidget(self.advanced_panel)
        self.scan_description = QtWidgets.QLabel()
        self.scan_description.setWordWrap(True)
        config_layout.addWidget(self.scan_description)
        row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton('Start RTL-SDR reception')
        self.start_button.clicked.connect(self.start)
        self.stop_button = QtWidgets.QPushButton('Stop')
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        config_layout.addLayout(row)
        self.state = QtWidgets.QLabel('Ready. Connect RTL-SDR and close other SDR applications before starting.')
        self.state.setWordWrap(True)
        config_layout.addWidget(self.state)
        left_layout.addWidget(config)
        spectrum_box = QtWidgets.QGroupBox('Spectrum — frequency offset from tuned center')
        self.plot_layout = QtWidgets.QVBoxLayout()
        spectrum_box.setLayout(self.plot_layout)
        self.spectrum_placeholder = QtWidgets.QLabel('Spectrum appears when reception starts')
        self.plot_layout.addWidget(self.spectrum_placeholder)
        self.spectrum_scale = QtWidgets.QLabel('Power: −120…0 dBFS · Frequency offset: −250…+250 kHz')
        self.spectrum_scale.setWordWrap(True)
        self.plot_layout.addWidget(self.spectrum_scale)
        self.spectrum_group = spectrum_box
        left_layout.addWidget(spectrum_box, 1)
        waterfall_box = QtWidgets.QGroupBox('Waterfall — frequency offset from tuned center')
        self.waterfall_layout = QtWidgets.QVBoxLayout(waterfall_box)
        self.waterfall_placeholder = QtWidgets.QLabel('Waterfall appears when reception starts')
        self.waterfall_layout.addWidget(self.waterfall_placeholder)
        self.waterfall_scale = QtWidgets.QLabel('Frequency offset: −250…+250 kHz · Newest samples at bottom')
        self.waterfall_scale.setWordWrap(True)
        self.waterfall_layout.addWidget(self.waterfall_scale)
        self.waterfall_group = waterfall_box
        left_layout.addWidget(waterfall_box, 1)
        self.splitter.addWidget(left)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 0, 0)
        self.decoded = QtWidgets.QLabel('Decoded packets')
        self.decoded.setWordWrap(True)
        right_layout.addWidget(self.decoded)
        self.table = QtWidgets.QTableWidget(0, len(DISPLAY_KEYS))
        self.table.setHorizontalHeaderLabels(
            ['Time', 'Raw payload hex'] + [label for _, label in DECODE_COLUMNS] +
            ['Status', 'Average signal (dBFS)', 'Peak signal (dBFS)', 'Received center frequency (Hz)'])
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet('QTableView {background: white; alternate-background-color: #eeeeee; color: #202020;}'
                                 'QTableView::item:selected {background: #cce4ff; color: #101010;}')
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.cellDoubleClicked.connect(self.show_packet_fields)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        self.table.setColumnWidth(0, 175)
        self.table.setColumnWidth(1, 590)
        for col, (_, label) in enumerate(DECODE_COLUMNS, 2):
            self.table.setColumnWidth(col, max(115, len(label) * 8 + 20))
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(DISPLAY_KEYS.index('message_type'), 180)
        self.table.setColumnWidth(2 + len(DECODE_COLUMNS), 260)
        for col in [DISPLAY_KEYS.index('signal_avg_dbfs'), DISPLAY_KEYS.index('signal_peak_dbfs')]:
            self.table.setColumnWidth(col, 170)
            self.table.horizontalHeaderItem(col).setToolTip(
                'Per-frame synchronized header/payload IQ power; dBFS, not calibrated dBm. Excludes preamble.')
        self.table.setColumnWidth(DISPLAY_KEYS.index('channel_frequency_hz'), 220)
        self.table.horizontalHeaderItem(12).setToolTip(
            'Transmitter placeholder byte; this is not measured receiver RSSI.')
        self.log_visual_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.log_visual_splitter.addWidget(self.table)
        self.distance_panel = QtWidgets.QWidget()
        distance_layout = QtWidgets.QVBoxLayout(self.distance_panel)
        distance_layout.setContentsMargins(0, 0, 0, 0)
        self.distance_scroll = QtWidgets.QScrollArea()
        self.distance_scroll.setWidgetResizable(True)
        self.distance_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.distance_scroll.setMinimumHeight(180)
        self._distance_scroll_maximum = 0
        self.distance_scroll.verticalScrollBar().rangeChanged.connect(self.anchor_distance_scroll)
        self.distance_window = DistanceView()
        self.distance_window.setMinimumWidth(360)
        self.distance_scroll.setWidget(self.distance_window)
        distance_layout.addWidget(self.distance_scroll, 1)
        distance_layout.addWidget(self.distance_window.controls)
        self.distance_window.controls.setMinimumHeight(32)
        self.distance_window.refresh_label_layout()
        self.log_visual_splitter.addWidget(self.distance_panel)
        self.log_visual_splitter.setStretchFactor(0, 2)
        self.log_visual_splitter.setStretchFactor(1, 1)
        self.log_visual_splitter.setSizes([520, 360])
        right_layout.addWidget(self.log_visual_splitter, 1)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([460, 1120])
        (ROOT/'logs').mkdir(exist_ok=True)
        self.log = ROOT/'logs'/datetime.now().strftime('receive-%Y%m%d-%H%M%S-%f.jsonl')
        self.set_language(self.preferences.value('language', 'en'))
        self.refresh_devices()
        self.apply_display_config()

    def show_distance_visualization(self):
        self.visual_action.setChecked(True)
        self.distance_window.set_language(self.language)
        self.distance_panel.show()
        self.log_visual_splitter.setSizes([max(200, self.height()-420), 360])
        self.distance_window.setFocus()

    def anchor_distance_scroll(self, minimum, maximum):
        scrollbar = self.distance_scroll.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= self._distance_scroll_maximum-2
        self._distance_scroll_maximum = maximum
        if was_at_bottom:
            scrollbar.setValue(maximum)

    def set_distance_visible(self, visible):
        if self.distance_window is not None:
            self.distance_panel.setVisible(visible)
            if visible:
                self.show_distance_visualization()

    def refresh_devices(self):
        if self.tb is not None:
            return
        selected = self.device_combo.currentData()
        initial = self.device_combo.count() == 0
        try:
            self.device_names = dict(discover_rtl_devices())
            self.discovery_error = ''
        except Exception as exc:
            self.device_names = {}
            self.discovery_error = str(exc)
        blocker = QtCore.QSignalBlocker(self.device_combo)
        self.device_combo.clear()
        self.device_combo.addItem('', None)
        # Keep manually configured arguments selectable, without claiming detection.
        for args in dict.fromkeys([*self.device_names, self.device]):
            self.device_combo.addItem('', args)
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(self.device if initial else selected)))
        del blocker
        self.update_device_captions()
        self.select_device(self.device_combo.currentIndex())
        self.update_reception_controls()

    def update_device_captions(self):
        chinese = self.language == 'zh'
        self.devices_title.setText('接收设备' if chinese else 'Receive device')
        self.refresh_button.setText('刷新' if chinese else 'Refresh')
        for index in range(self.device_combo.count()):
            args = self.device_combo.itemData(index)
            if args is None:
                self.device_combo.setItemText(index, '请选择接收设备…' if chinese else 'Select receive device…')
                continue
            name = self.device_names.get(args)
            caption = (f'{name} · {args}' if name else
                       f'RTL-SDR · {args} ' + ('（未检测到，手动配置）' if chinese else '(not detected; configured)'))
            self.device_combo.setItemText(index, caption)
            self.device_combo.setItemData(index, caption, QtCore.Qt.ToolTipRole)
        self.device_note.setText(('设备枚举失败，可用“设备 → 配置设备”手动指定。' if chinese else
                                 'Device discovery failed. Use Device → Configure device for manual arguments.')
                                if self.discovery_error else
                                (('未检测到 RTL-SDR；请连接设备后刷新。' if chinese else
                                  'No RTL-SDR detected. Connect a device and refresh.') if not self.device_names else ''))
        self.device_note.setToolTip(self.discovery_error)
        self.device_note.setVisible(bool(self.device_note.text()))

    def select_device(self, index):
        if self.tb is not None:
            return
        args = self.device_combo.itemData(index)
        if args is not None:
            self.device = args
            self.preferences.setValue('receiver_device', args)
            self.device_combo.setToolTip(self.device_combo.itemText(index))
        else:
            self.device_combo.setToolTip('')
        self.update_reception_controls()

    def update_reception_controls(self):
        running = self.tb is not None
        selected = self.device_combo.currentData() is not None
        self.start_button.setEnabled(not running and selected)
        self.start_action.setEnabled(not running and selected)
        self.stop_button.setEnabled(running)
        self.stop_action.setEnabled(running)
        for widget in (self.configure_action, self.refresh_action, self.refresh_button, self.device_combo):
            widget.setEnabled(not running)
        for key, entry in self.entries.items():
            entry.setEnabled(not running or key == 'gain')

    def show_measurement_help(self):
        QtWidgets.QMessageBox.information(self, '信号强度测量' if self.language == 'zh' else 'Signal measurement',
            ('平均强度 = 10 log10(mean(|IQ|²))；峰值强度 = 10 log10(max(|IQ|²))。\n'
             '测量同步后的 LoRa 头部/载荷 IQ，位于 FFT 之前，不含前导码。\n'
             '单位为相对归一化复数幅度 1 的 dBFS，并非校准后的 dBm。\n'
             '缺少完整匹配报文时不显示测量值。' if self.language == 'zh' else
             'Average = 10 log10(mean(|IQ|²)); peak = 10 log10(max(|IQ|²)).\n'
             'Measured on synchronized LoRa header/payload IQ before FFT; excludes preamble.\n'
             'Units: dBFS relative to normalized complex sample amplitude 1, not calibrated dBm.\n'
             'No measurement is shown when a complete matching frame is unavailable.'))

    def tr(self, key):
        return self.translations[self.language][key]

    def set_language(self, language):
        if language not in self.translations:
            language = 'en'
        self.language = language
        self.preferences.setValue('language', language)
        self.setWindowTitle('GNU Radio — RTL-SDR LoRa / LifeBeacon' if language == 'en' else 'GNU Radio — RTL-SDR LoRa / LifeBeacon（中文）')
        self.device_menu.setTitle(self.tr('device'))
        self.logs_menu.setTitle(self.tr('logs'))
        self.open_logs_action.setText('读取往期日志…' if language == 'zh' else 'Open historical logs…')
        self.statistics_action.setText('当前会话范围统计…' if language == 'zh' else 'Current session range statistics…')
        self.help_menu.setTitle(self.tr('help'))
        self.language_menu.setTitle(self.tr('language'))
        self.configure_action.setText(self.tr('configure'))
        self.refresh_action.setText('Refresh devices' if language == 'en' else '刷新设备列表')
        self.start_action.setText(self.tr('start'))
        self.stop_action.setText(self.tr('stop'))
        self.export_menu.setTitle('Export current session' if language == 'en' else '导出当前会话')
        self.history_action.setText('Save an existing log file as…' if language == 'en' else '另存历史日志…')
        self.exit_action.setText(self.tr('exit'))
        self.edit_display_action.setText('Edit live log display…' if language == 'en' else '编辑实时日志显示…')
        self.measurement_action.setText('Signal measurement details' if language == 'en' else '信号强度测量说明')
        self.config_group.setTitle(self.tr('config'))
        for key, label in self.form_labels.items(): label.setText(self.tr(key))
        self.advanced_title.setText('RF / decoder settings' if language == 'en' else '射频与解码参数')
        self.sync_verify.setText('Strict sync-word verification (recommended)' if language == 'en' else
                                 '严格校验同步字（推荐）')
        self.scan_description.setText('25 kHz channel spacing · SF7–12 · Explicit header' if language == 'en' else
                                      '25 kHz 信道间隔 · SF7–12 自动扫描 · 显式头')
        self.start_button.setText(self.tr('start_button')); self.stop_button.setText(self.tr('stop_button'))
        self.decoded.setText(self.tr('decoded') + f' — {len(self.records)}')
        self.update_device_captions()
        if self.tb is None:
            self.state.setText('Ready. Select a device and start reception.' if language == 'en' else
                               '就绪。选择接收设备后开始接收。')
        self.spectrum_group.setTitle(self.tr('spectrum')); self.waterfall_group.setTitle(self.tr('waterfall'))
        self.visual_action.setText('Distance visualization' if language == 'en' else '距离可视化')
        self.visual_menu.setTitle('Visualization' if language == 'en' else '可视化')
        if self.distance_window is not None:
            self.distance_window.set_language(language)
        self.english_action.setCheckable(True); self.chinese_action.setCheckable(True)
        self.english_action.setChecked(language == 'en'); self.chinese_action.setChecked(language == 'zh')
        headers = (['Time', 'Raw payload hex'] + [label for _, label in DECODE_COLUMNS] +
                   ['Status', 'Average signal (dBFS)', 'Peak signal (dBFS)']) if language == 'en' else (
                   ['时间', '原始十六进制报文'] + [DECODE_LABELS_ZH[key] for key, _ in DECODE_COLUMNS] +
                   ['状态', '平均强度 (dBFS)', '峰值强度 (dBFS)'])
        self.table.setHorizontalHeaderLabels(headers + ['Received center frequency (Hz)' if language == 'en' else '接收中心频率 (Hz)'])
        self.table.horizontalHeaderItem(len(DISPLAY_KEYS)-1).setToolTip(
            '解码信道中心，不是精确载波频偏测量' if language == 'zh' else
            'Decoder channel center, not a precise carrier-frequency estimate')
        self.table.setToolTip('双击报文查看逐字段字节偏移、原始值及解码值' if language == 'zh' else
                              'Double-click a packet for field offsets, raw bytes and decoded values')
        self.spectrum_placeholder.setText('Spectrum appears when reception starts' if language == 'en' else '开始接收后显示频谱')
        self.waterfall_placeholder.setText('Waterfall appears when reception starts' if language == 'en' else '开始接收后显示瀑布图')
        self.update_scale_captions()
        self.apply_display_config()

    def update_scale_captions(self):
        try:
            half_band = float(self.entries['sample_rate'].text())/2000
        except ValueError:
            half_band = 1200
        if self.language == 'zh':
            self.spectrum_scale.setText(f'强度：−120…0 dBFS · 频率偏移：−{half_band:g}…+{half_band:g} kHz')
            self.waterfall_scale.setText(f'频率偏移：−{half_band:g}…+{half_band:g} kHz · 最新采样在底部')
        else:
            self.spectrum_scale.setText(f'Power: −120…0 dBFS · Frequency offset: −{half_band:g}…+{half_band:g} kHz')
            self.waterfall_scale.setText(f'Frequency offset: −{half_band:g}…+{half_band:g} kHz · Newest samples at bottom')

    def apply_gain_entry(self):
        try:
            value = float(self.entries['gain'].text())
            if not math.isfinite(value) or not 0 <= value <= 50:
                raise ValueError('Gain must be between 0 and 50 dB')
            self.apply_gain(value)
        except Exception as exc:
            self.state.setText(str(exc))

    def apply_gain(self, value):
        requested_gain = value
        if self.tb is not None:
            self.tb.set_gain(value)
            if hasattr(self.tb.source, 'get_gain'):
                value = self.tb.source.get_gain()
        blocker = QtCore.QSignalBlocker(self.gain_slider)
        self.gain_slider.setValue(round(value))
        del blocker
        self.entries['gain'].setText(f'{value:g}')
        if self.tb is not None and self.active_params is not None:
            params = dict(self.active_params, gain=requested_gain)
            self.record_receiver_configuration(params, 'gain_changed')
        self.state.setText(('Gain applied: ' if self.language == 'en' else '已应用增益：') + f'{value:g} dB')

    def _gain_slider_changed(self, value):
        try:
            self.apply_gain(value)
        except Exception as exc:
            self.state.setText(('Gain update failed: ' if self.language == 'en' else '增益更新失败：') + str(exc))

    def start(self):
        if self.tb is not None:
            return
        try:
            if self.device_combo.currentData() is None:
                raise ValueError('请先选择接收设备' if self.language == 'zh' else 'Select a receive device first')
            p = {key: (int(e.text(), 0) if key == 'sync' else float(e.text())) for key, e in self.entries.items()}
            p['sf'] = 7
            p['sync_auto'] = not self.sync_verify.isChecked()
            for key in ['frequency', 'sample_rate', 'scan_bandwidth', 'bw']:
                if not p[key].is_integer():
                    raise ValueError(key + ' must be an integer')
                p[key] = int(p[key])
            if p['bw'] not in [125000, 250000, 500000] or not 7 <= p['sf'] <= 12 or not 0 <= p['sync'] <= 255:
                raise ValueError('Invalid LoRa bandwidth, SF or sync word')
            if p['sample_rate'] < 2*p['bw'] or p['sample_rate'] > 2400000:
                raise ValueError('Use RTL sample rate between twice BW and 2400000')
            if not math.isfinite(p['gain']) or not 0 <= p['gain'] <= 50:
                raise ValueError('Gain must be between 0 and 50 dB')
            if not p['bw'] <= p['scan_bandwidth'] <= p['sample_rate']*0.8:
                raise ValueError('Scan bandwidth must be >= LoRa BW and <= 80% of RTL sample rate')
            p['device'] = self.device
            if hasattr(self, 'hardware_params'):
                p.update(self.hardware_params())
            self.tb = Receiver(p, self.events.packet.emit)
            self.update_scale_captions()
            for sink, target_layout in [(self.tb.spectrum, self.plot_layout),
                                        (self.tb.waterfall, self.waterfall_layout)]:
                widget = sip.wrapinstance(sink.qwidget(), QtWidgets.QWidget)
                widget.setMinimumSize(0, 0)
                widget.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
                target_layout.insertWidget(1, widget, 1)
                self.plots.append(widget)
            self.spectrum_placeholder.hide()
            self.waterfall_placeholder.hide()
            self.tb.start()
            self.record_receiver_configuration(p, 'reception_started')
            self.update_reception_controls()
            self.state.setText('Receiving — awaiting LoRa PHY and LB CRC verification' if self.language == 'en'
                               else '接收中 — 等待 LoRa PHY 与 LB CRC 校验')
        except Exception as exc:
            self.stop()
            self.state.setText(('Receiver could not start: ' if self.language == 'en' else '无法启动接收：') + str(exc))

    def stop(self):
        if self.tb:
            self.tb.stop()
            self.tb.wait()
            self.tb = None
        # Deliver queued worker events before draining pending candidates.
        QtCore.QCoreApplication.sendPostedEvents(self, QtCore.QEvent.MetaCall)
        QtCore.QCoreApplication.sendPostedEvents(self.packet_merger, QtCore.QEvent.MetaCall)
        self.packet_merger.flush(force=True)
        for widget in self.plots:
            widget.setParent(None)
            widget.deleteLater()
        self.plots.clear()
        self.spectrum_placeholder.show()
        self.waterfall_placeholder.show()
        self.update_reception_controls()
        self.state.setText('Stopped' if self.language == 'en' else '已停止')

    def configured_params(self):
        params = {key: (int(entry.text(), 0) if key == 'sync' else float(entry.text()))
                  for key, entry in self.entries.items()}
        params.update(device=self.device, sf=7)
        return params

    def configuration_header(self, params, event):
        actual_gain = None
        if self.tb is not None and hasattr(self.tb.source, 'get_gain'):
            try:
                actual_gain = float(self.tb.source.get_gain())
            except Exception:
                pass
        return dict(record_type='receiver_config', log_schema_version=2,
            config_id=uuid.uuid4().hex, time=datetime.now().astimezone().isoformat(timespec='milliseconds'),
            event=event, receiver_config=dict(
                device=params['device'], device_name=self.device_names.get(params['device']),
                center_frequency_hz=params['frequency'], scan_bandwidth_hz=params['scan_bandwidth'],
                sample_rate_sps=params['sample_rate'], lora_bandwidth_hz=params['bw'],
                gain_mode='manual', gain_requested_db=(None if params['device'].startswith('hackrf=') else params['gain']),
                gain_readback_db=actual_gain,
                hackrf_lna_db=params.get('hackrf_lna'), hackrf_vga_db=params.get('hackrf_vga'),
                hackrf_amp=params.get('hackrf_amp'),
                frequency_correction_ppm=params['ppm'], scan_channel_spacing_hz=SCAN_CHANNEL_SPACING_HZ,
                spreading_factors=list(range(7, 13)), sync_word=params['sync'], sync_word_hex=f"0x{params['sync']:02X}",
                sync_word_mode='strict' if not params.get('sync_auto') else 'wildcard recovery',
                channel_filter_cutoff_ratio=CHANNEL_FILTER_CUTOFF_RATIO,
                channel_filter_transition_ratio=CHANNEL_FILTER_TRANSITION_RATIO,
                header_mode='explicit', coding_rate='from LoRa header', phy_crc_required=True,
                lb_crc_required=True, soft_decoding=True, max_log_approx=True, ldro_mode='auto',
                signal_unit='dBFS', cross_channel_merge_window_ms=750))

    def record_receiver_configuration(self, params, event):
        header = self.configuration_header(params, event)
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(header, ensure_ascii=False)+'\n')
        self.active_params = dict(params)
        self.active_config = header
        self.active_log_path = self.log
        self.config_history.append(header)
        return header

    def exported_configurations(self):
        return self.config_history or [self.configuration_header(self.configured_params(), 'configured_only')]

    def on_packet(self, record):
        if not phy_crc_passed(record):
            return
        if self.active_config is None or self.active_log_path != self.log:
            self.record_receiver_configuration(self.configured_params(), 'configured_only')
        record = decode_record(record)
        record['record_type'] = 'packet'
        record['receiver_config_id'] = self.active_config['config_id']
        record.setdefault('_received_monotonic', time.monotonic())
        record['receiver_device'] = self.device
        record['receiver_gain_db_at_display'] = (None if self.device.startswith('hackrf=') else float(self.entries['gain'].text()))
        if self.device.startswith('hackrf='):
            record['receiver_hackrf_gains'] = {k: self.active_params.get(k)
                for k in ('hackrf_lna', 'hackrf_vga', 'hackrf_amp')}
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        self.records.append(record)
        if self.display_config['show_failed'] or record['status'] == 'PHY CRC OK / LB OK':
            self.append_display_row(record)
        self.decoded.setText((f'Decoded packets — {len(self.records)}'
                             if self.language == 'en' else f'已接收报文 — {len(self.records)}'))
        self.state.setText(record['status'])
        if self.distance_window is not None:
            self.distance_window.update_device(record)

    def append_display_row(self, record):
        row = self.table.rowCount()
        self.table.insertRow(row)
        fields = record.get('fields') or {}
        segments = {segment['key']: segment for segment in record.get('payload_fields', [])}
        values = [display_value(record, key, self.language) for key in DISPLAY_KEYS]
        device_id = fields.get('device_id')
        mode = self.display_config['color']
        if mode == 'device' and device_id is not None:
            background = device_background(device_id)
        else:
            background = QtGui.QColor('#eeeeee' if mode == 'alternate' and row % 2 else '#ffffff')
        for col, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            font = item.font()
            font.setBold(DISPLAY_KEYS[col] in self.display_config['bold'])
            item.setFont(font)
            item.setBackground(background)
            segment = segments.get(DISPLAY_KEYS[col])
            item.setToolTip(text if segment is None else
                            f"{segment['field']} [{segment['offset_start']:02d}-{segment['offset_end']:02d}] "
                            f"{segment['raw_hex']} → {text}")
            if col == 0:
                item.setData(QtCore.Qt.UserRole, record)
            self.table.setItem(row, col, item)
        if self.table.rowCount() > 500:
            self.table.removeRow(0)
            if mode == 'alternate':
                for r in range(self.table.rowCount()):
                    brush = QtGui.QBrush(QtGui.QColor('#eeeeee' if r % 2 else '#ffffff'))
                    for c in range(self.table.columnCount()):
                        self.table.item(r, c).setBackground(brush)
        self.table.scrollToBottom()

    def show_packet_fields(self, row, column=0):
        item = self.table.item(row, 0)
        if item is None:
            return
        record = item.data(QtCore.Qt.UserRole)
        self.show_record_fields(record)

    def show_record_fields(self, record):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('报文逐字段解码' if self.language == 'zh' else 'Packet field decoding')
        dialog.resize(900, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setFont(QtGui.QFont('Consolas', 10))
        lines = [record['time'], record['raw_payload_hex'], record['status'], '']
        if record.get('receiver_config_id'):
            lines.append('receiver_config_id: '+record['receiver_config_id'])
        for segment in record.get('payload_fields', []):
            start, end = segment['offset_start'], segment['offset_end']
            offset = f'[{start:02d}]' if start == end else f'[{start:02d}-{end:02d}]'
            value = segment['value']
            if segment['key'] == 'message_type':
                value = f'{value} ({message_type_label(value, self.language)})'
            lines.append(f"[rx] {segment['field']:<23} {offset:<8} {segment['raw_hex']:<28} => {value}")
        fields = record.get('fields') or {}
        if fields and (fields.get('hr') is None or fields.get('spo2') is None):
            lines += ['', ('生理标志未置位或原始字节为 FF 时，测量值显示为“—”，原始字节仍保留。'
                           if self.language == 'zh' else
                           'Measurements with a cleared validity flag or raw FF show as —; raw bytes are retained.')]
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.button(QtWidgets.QDialogButtonBox.Close).setText('关闭' if self.language == 'zh' else 'Close')
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()

    def apply_display_config(self):
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        for position, key in enumerate(self.display_config['order']):
            logical = DISPLAY_KEYS.index(key)
            header.moveSection(header.visualIndex(logical), position)
            self.table.setColumnHidden(logical, key in self.display_config['hidden'])
            font = self.table.horizontalHeaderItem(logical).font()
            font.setBold(key in self.display_config['bold'])
            self.table.horizontalHeaderItem(logical).setFont(font)
        visible = [record for record in self.records if self.display_config['show_failed']
                   or record['status'] == 'PHY CRC OK / LB OK']
        self.table.setRowCount(0)
        for record in visible[-500:]:
            self.append_display_row(record)

    def edit_live_display(self):
        chinese = self.language == 'zh'
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('编辑实时日志显示' if chinese else 'Edit live log display')
        dialog.resize(650, 640)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel('选择显示列和加粗；使用上下按钮调整顺序。' if chinese else
                                         'Choose visible/bold columns; use Up/Down to change order.'))
        columns = QtWidgets.QTableWidget(len(DISPLAY_KEYS), 3)
        columns.setHorizontalHeaderLabels(['内容项', '显示', '加粗'] if chinese else ['Column', 'Show', 'Bold'])
        columns.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        columns.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        columns.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        columns.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for row, key in enumerate(self.display_config['order']):
            label = self.table.horizontalHeaderItem(DISPLAY_KEYS.index(key)).text()
            item = QtWidgets.QTableWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, key)
            columns.setItem(row, 0, item)
            for col, checked in [(1, key not in self.display_config['hidden']), (2, key in self.display_config['bold'])]:
                check = QtWidgets.QTableWidgetItem()
                check.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsSelectable)
                check.setCheckState(QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked)
                columns.setItem(row, col, check)
        layout.addWidget(columns)
        buttons_row = QtWidgets.QHBoxLayout()
        def move(delta):
            row = columns.currentRow()
            target = row + delta
            if row < 0 or not 0 <= target < columns.rowCount():
                return
            for col in range(3):
                a, b = columns.takeItem(row, col), columns.takeItem(target, col)
                columns.setItem(row, col, b)
                columns.setItem(target, col, a)
            columns.selectRow(target)
        for label, delta in [('上移' if chinese else 'Up', -1), ('下移' if chinese else 'Down', 1)]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda _, d=delta: move(d))
            buttons_row.addWidget(button)
        layout.addLayout(buttons_row)
        form = QtWidgets.QFormLayout()
        colors = QtWidgets.QComboBox()
        for label, key in [('设备颜色' if chinese else 'By device', 'device'),
                           ('白灰交替' if chinese else 'White/gray alternate', 'alternate'),
                           ('全白' if chinese else 'All white', 'white')]:
            colors.addItem(label, key)
        colors.setCurrentIndex(max(0, colors.findData(self.display_config['color'])))
        form.addRow('行颜色' if chinese else 'Row colors', colors)
        failed = QtWidgets.QCheckBox('显示 LB 解码失败报文' if chinese else 'Show LB decode failures')
        failed.setChecked(self.display_config['show_failed'])
        form.addRow(failed)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText('确定' if chinese else 'OK')
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消' if chinese else 'Cancel')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            order, hidden, bold = [], [], []
            for row in range(columns.rowCount()):
                key = columns.item(row, 0).data(QtCore.Qt.UserRole)
                order.append(key)
                if columns.item(row, 1).checkState() != QtCore.Qt.Checked:
                    hidden.append(key)
                if columns.item(row, 2).checkState() == QtCore.Qt.Checked:
                    bold.append(key)
            if len(hidden) == len(order):
                QtWidgets.QMessageBox.warning(self, dialog.windowTitle(),
                    '请至少保留一列。' if chinese else 'Keep at least one visible column.')
                return
            self.display_config = dict(order=order, hidden=hidden, bold=bold,
                                       color=colors.currentData(), show_failed=failed.isChecked())
            self.preferences.setValue('display_config', json.dumps(self.display_config))
            self.apply_display_config()

    def configure_device(self):
        if self.tb is not None:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('RTL-SDR device configuration' if self.language == 'en' else 'RTL-SDR 设备配置')
        form = QtWidgets.QFormLayout(dialog)
        device = QtWidgets.QLineEdit(self.device)
        form.addRow('Device arguments (index or serial)' if self.language == 'en' else '设备参数（索引或序列号）', device)
        note = QtWidgets.QLabel('Examples: rtl=0 or rtl=00000001.\nStop reception before changing devices.'
                               if self.language == 'en' else '示例：rtl=0 或 rtl=00000001。\n更换设备前请停止接收。')
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText('确定' if self.language == 'zh' else 'OK')
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消' if self.language == 'zh' else 'Cancel')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            value = device.text().strip()
            if not value.startswith('rtl=') or ',' in value or ' ' in value or len(value) <= 4:
                QtWidgets.QMessageBox.warning(self, 'Invalid device', 'Use rtl= followed by a device index or serial.')
                return
            self.device = value
            self.preferences.setValue('receiver_device', value)
            self.refresh_devices()
            self.device_combo.setCurrentIndex(self.device_combo.findData(value))

    def log_browser(self, records=None):
        return LogBrowserDialog(self, DISPLAY_KEYS, display_value, decode_record, phy_crc_passed,
                                device_background, records)

    def open_historical_logs(self):
        browser = self.log_browser()
        if browser.open_files():
            browser.exec_()
        browser.deleteLater()

    def show_session_statistics(self):
        browser = self.log_browser(list(self.records))
        browser.tabs.setCurrentIndex(1)
        browser.exec_()
        browser.deleteLater()

    def export_jsonl(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export session log',
            str(ROOT/'logs'/'session.jsonl'), 'JSONL (*.jsonl)')
        if path:
            try:
                entries = self.exported_configurations() + self.records
                Path(path).write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in entries), encoding='utf-8')
                self.state.setText('Log saved: ' + path)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, 'Export failed', str(exc))

    def export_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export session CSV',
            str(ROOT/'logs'/'session.csv'), 'CSV (*.csv)')
        if path:
            try:
                keys = ['record_type', 'receiver_config_id', 'receiver_config_json', 'config_event',
                    'time', 'raw_payload_hex'] + [key for key, _ in DECODE_COLUMNS] + [
                    'status', 'signal_avg_dbfs', 'signal_peak_dbfs', 'signal_samples', 'signal_scope',
                    'receiver_device', 'receiver_gain_db_at_display', 'channel_frequency_hz',
                    'spreading_factor', 'lora_bandwidth_hz', 'message_type_label', 'phy_crc_valid', 'payload_fields_json']
                with Path(path).open('w', newline='', encoding='utf-8-sig') as stream:
                    writer = csv.DictWriter(stream, fieldnames=keys)
                    writer.writeheader()
                    for header in self.exported_configurations():
                        writer.writerow(dict(record_type='receiver_config', receiver_config_id=header['config_id'],
                            receiver_config_json=json.dumps(header['receiver_config'], ensure_ascii=False),
                            config_event=header['event'], time=header['time']))
                    for record in self.records:
                        flat = {key: record.get(key) for key in keys}
                        flat['record_type'] = 'packet'
                        flat.update(record.get('fields') or {})
                        flat['phy_crc_valid'] = phy_crc_passed(record)
                        flat['payload_fields_json'] = json.dumps(record.get('payload_fields', []), ensure_ascii=False)
                        writer.writerow({key: flat.get(key) for key in keys})
                self.state.setText('CSV saved: ' + path)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, 'Export failed', str(exc))

    def save_existing_log(self):
        source, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Choose existing log', str(ROOT/'logs'), 'JSONL (*.jsonl)')
        if not source:
            return
        target, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save log as', Path(source).name, 'JSONL (*.jsonl)')
        if target:
            try:
                if Path(source).resolve() != Path(target).resolve():
                    shutil.copyfile(source, target)
                self.state.setText('Log saved: ' + target)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, 'Export failed', str(exc))

    def closeEvent(self, event):
        self.stop()
        if self.distance_window is not None:
            self.distance_window.close()
        event.accept()


def self_test():
    """Digital loopback only: exercises binary NUL bytes, PHY tags and LB CRC."""
    name = b'Lambert CHEN'
    raw = b'LB\x01\x01\x01' + (0x12345678).to_bytes(4, 'little') + bytes([len(name)]) + name
    raw += bytes([3, 72, 98, 0, 0, 85, 17, 128, 0])
    raw += binascii.crc_hqx(raw, 0xffff).to_bytes(2, 'little')
    records = []
    # The whitening message API uses a C++ string; use a tagged byte-stream chain for binary data.
    generator = gr.top_block()
    payload = blocks.vector_source_b(list(raw)*4, False)
    tagged = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, len(raw), 'packet_len')
    whitening = lora_sdr.whitening(False, True, ',', 'packet_len')
    header = lora_sdr.header(False, True, 1)
    crc = lora_sdr.add_crc(True)
    hamming = lora_sdr.hamming_enc(1, 7)
    interleave = lora_sdr.interleaver(1, 7, 2, 125000)
    gray = lora_sdr.gray_demap(7)
    modulate = lora_sdr.modulate(7, 1000000, 125000, [0x12], 10000, 8)
    sink = blocks.vector_sink_c()
    generator.connect(payload, tagged, whitening, header, crc, hamming, interleave, gray, modulate, sink)
    generator.run()
    generated = np.array(sink.data(), np.complex64)
    if len(generated) % 4:
        raise RuntimeError('Synthetic frames have unequal sample lengths')
    amplitudes = [1.0, 0.5, 0.25, 0.125]
    for frame, amplitude in zip(np.split(generated, 4), amplitudes):
        frame *= amplitude
    iq = np.concatenate([np.zeros(4096, np.complex64), generated, np.zeros(20000, np.complex64)])
    params = dict(frequency=433775000, sample_rate=1000000, bw=125000, sf=7, sync=0x12)
    receiver = Receiver(params, records.append, False, blocks.vector_source_c(iq.tolist(), False))
    receiver.run()
    valid = [r for r in records if r['status'] == 'PHY CRC OK / LB OK' and r['raw_payload_hex'] == raw.hex(' ').upper()]
    if len(valid) != 4:
        raise RuntimeError('Digital loopback failed: ' + repr(records))
    for record, amplitude in zip(valid, amplitudes):
        expected = 20*math.log10(amplitude)
        if abs(record.get('signal_avg_dbfs', float('inf')) - expected) > 0.25:
            raise RuntimeError('Per-frame average power attribution failed: ' + repr(record))
        if abs(record.get('signal_peak_dbfs', float('inf')) - expected) > 0.5:
            raise RuntimeError('Per-frame peak power attribution failed: ' + repr(record))
    # Exercise band scanning on two simultaneous carriers at different offsets.
    t = np.arange(len(iq))/params['sample_rate']
    wide_iq = iq * (np.exp(2j*np.pi*50000*t) + 0.5*np.exp(-2j*np.pi*50000*t))
    scan_records = []
    scanner = Receiver(dict(params, scan_bandwidth=250000), scan_records.append, False,
                       blocks.vector_source_c(wide_iq.astype(np.complex64).tolist(), False))
    scanner.run()
    frequencies = {r.get('channel_frequency_hz') for r in scan_records
                   if r['status'] == 'PHY CRC OK / LB OK'}
    if not {433725000, 433825000}.issubset(frequencies):
        raise RuntimeError('Two-carrier scan failed: ' + repr(scan_records))
    report = dict(test='synthetic digital loopback, not live RF', matching_packets=len(valid), records=records,
                  scan_test=dict(simultaneous_carriers_hz=sorted(frequencies), records=scan_records))
    (ROOT/'self-test-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--gui-smoke-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        app = QtWidgets.QApplication(sys.argv)
        app.setFont(QtGui.QFont('Segoe UI', 9))
        window = Window()
        window.show()
        if args.gui_smoke_test:
            QtCore.QTimer.singleShot(1500, app.quit)
        sys.exit(app.exec_())
