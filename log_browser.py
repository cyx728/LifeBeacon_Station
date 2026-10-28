"""Historical packet browser and statistics dialog; isolated from live reception."""
import csv
import json
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from log_analysis import read_log_files, range_statistics


STATS_COLUMNS = [
    ('device_id', '设备 ID', 'Device ID'), ('name', '姓名', 'Name'), ('packets', '包数', 'Packets'),
    ('signal_valid', '强度有效数', 'Signal samples'),
    ('signal_max', '强度 MAX (dBFS)', 'Signal MAX (dBFS)'),
    ('signal_min', '强度 MIN (dBFS)', 'Signal MIN (dBFS)'),
    ('signal_avg', '强度 AVG (dBFS)', 'Signal AVG (dBFS)'),
    ('hr_valid', '心率有效数', 'HR valid count'), ('hr_valid_percent', '心率有效率 (%)', 'HR valid (%)'),
    ('hr_avg', '心率 AVG (bpm)', 'HR AVG (bpm)'), ('hr_min', '心率 MIN', 'HR MIN'),
    ('hr_max', '心率 MAX', 'HR MAX'), ('hr_values', '心率有效值 (bpm)', 'Valid HR values (bpm)'),
    ('spo2_valid', '血氧有效数', 'SpO2 valid count'), ('spo2_valid_percent', '血氧有效率 (%)', 'SpO2 valid (%)'),
    ('spo2_avg', '血氧 AVG (%)', 'SpO2 AVG (%)'), ('spo2_min', '血氧 MIN', 'SpO2 MIN'),
    ('spo2_max', '血氧 MAX', 'SpO2 MAX'), ('spo2_values', '血氧有效值 (%)', 'Valid SpO2 values (%)'),
]


class PacketLogModel(QtCore.QAbstractTableModel):
    def __init__(self, browser):
        super().__init__(browser)
        self.browser = browser

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.browser.records)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.browser.columns)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole:
            return self.browser.headers[section] if orientation == QtCore.Qt.Horizontal else str(section+1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        browser = self.browser
        record = browser.records[index.row()]
        if role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole):
            text = browser.format_value(record, browser.columns[index.column()], browser.language)
            if role == QtCore.Qt.ToolTipRole and record.get('_source_file'):
                text += f"\n{record['_source_file']}:{record['_source_line']}"
            return text
        if role == QtCore.Qt.FontRole and browser.columns[index.column()] in browser.bold:
            font = QtGui.QFont()
            font.setBold(True)
            return font
        if role == QtCore.Qt.BackgroundRole:
            device_id = (record.get('fields') or {}).get('device_id')
            if browser.color_mode == 'device' and device_id is not None:
                return browser.device_color(device_id)
            return QtGui.QColor('#eeeeee' if browser.color_mode == 'alternate' and index.row()%2 else '#ffffff')


class LogBrowserDialog(QtWidgets.QDialog):
    def __init__(self, parent, columns, format_value, decode, phy_passed, device_color, records=None):
        super().__init__(parent)
        self.language = parent.language
        self.zh = self.language == 'zh'
        self.columns = columns
        self.headers = [parent.table.horizontalHeaderItem(i).text() for i in range(len(columns))]
        self.format_value, self.decode, self.phy_passed, self.device_color = format_value, decode, phy_passed, device_color
        self.bold = parent.display_config['bold'][:]
        self.color_mode = parent.display_config['color']
        self.records = list(records or [])
        self.sources = []
        self.configurations = list(parent.config_history) if records is not None else []
        self.issues = []
        self.report = None
        self.setWindowTitle(self.t('历史日志与范围统计', 'Log browser and range statistics'))
        self.resize(1250, 780)
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        self.open_button = QtWidgets.QPushButton(self.t('读取日志…', 'Open logs…'))
        self.open_button.clicked.connect(self.open_files)
        toolbar.addWidget(self.open_button)
        self.config_button = QtWidgets.QPushButton(self.t('接收端配置…', 'Receiver configurations…'))
        self.config_button.setEnabled(bool(self.configurations))
        self.config_button.clicked.connect(self.show_configurations)
        toolbar.addWidget(self.config_button)
        self.source_label = QtWidgets.QLabel(self.t('当前会话快照', 'Current session snapshot') if records is not None else '')
        self.source_label.setWordWrap(True)
        toolbar.addWidget(self.source_label, 1)
        layout.addLayout(toolbar)
        self.load_summary = QtWidgets.QLabel()
        self.load_summary.setWordWrap(True)
        layout.addWidget(self.load_summary)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.packets = QtWidgets.QTableView()
        self.model = PacketLogModel(self)
        self.packets.setModel(self.model)
        self.packets.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.packets.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.packets.setWordWrap(False)
        self.packets.setStyleSheet('QTableView {color: #202020;}')
        self.packets.doubleClicked.connect(lambda index: parent.show_record_fields(self.records[index.row()]))
        for i in range(len(columns)):
            self.packets.setColumnWidth(i, parent.table.columnWidth(i))
        for position, key in enumerate(parent.display_config['order']):
            header = self.packets.horizontalHeader()
            logical = columns.index(key)
            header.moveSection(header.visualIndex(logical), position)
        # All protocol columns remain available in history, regardless of live hidden columns.
        self.tabs.addTab(self.packets, self.t('历史报文（全部行）', 'Packets (all rows)'))
        stats_page = QtWidgets.QWidget()
        stats_layout = QtWidgets.QVBoxLayout(stats_page)
        controls = QtWidgets.QHBoxLayout()
        self.range_mode = QtWidgets.QComboBox()
        self.range_mode.addItem(self.t('日志行号（从 1 开始）', 'Log row number (starts at 1)'), 'row')
        self.range_mode.addItem(self.t('报文 sequence（0–255）', 'Packet sequence (0–255)'), 'sequence')
        controls.addWidget(self.range_mode)
        self.start_input, self.end_input = QtWidgets.QSpinBox(), QtWidgets.QSpinBox()
        controls.addWidget(QtWidgets.QLabel(self.t('从', 'From')))
        controls.addWidget(self.start_input)
        controls.addWidget(QtWidgets.QLabel(self.t('至', 'To')))
        controls.addWidget(self.end_input)
        self.signal_mode = QtWidgets.QComboBox()
        self.signal_mode.addItem(self.t('强度：报文平均值 (dBFS)', 'Signal: packet average (dBFS)'), 'signal_avg_dbfs')
        self.signal_mode.addItem(self.t('强度：报文峰值 (dBFS)', 'Signal: packet peak (dBFS)'), 'signal_peak_dbfs')
        controls.addWidget(self.signal_mode)
        stats_layout.addLayout(controls)
        note = QtWidgets.QLabel(self.t(
            '范围含两端。sequence 起点大于终点表示跨 255→0；不同发送周期中的同序号均计入。\n'
            '仅 PHY 与 LB 校验通过的报文参与设备统计；心率/血氧需有效标志且非 FF。\n'
            '强度来自接收端 dBFS，非占位 RSSI 或校准 dBm；AVG 为 dBFS 算术平均。',
            'Inclusive range. Sequence start > end wraps through 255→0; matching sequences from all cycles count.\n'
            'Device statistics use PHY/LB-valid packets; vital values require a valid flag and non-FF byte.\n'
            'Signal is measured dBFS, not placeholder RSSI or calibrated dBm; AVG is arithmetic mean of dBFS.'))
        note.setWordWrap(True)
        stats_layout.addWidget(note)
        self.stats_summary = QtWidgets.QLabel()
        self.stats_summary.setWordWrap(True)
        stats_layout.addWidget(self.stats_summary)
        self.stats_table = QtWidgets.QTableWidget(0, len(STATS_COLUMNS))
        self.stats_table.setHorizontalHeaderLabels([zh if self.zh else en for _, zh, en in STATS_COLUMNS])
        self.stats_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.stats_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.stats_table.setWordWrap(False)
        for column, (key, _, _) in enumerate(STATS_COLUMNS):
            self.stats_table.setColumnWidth(column, 240 if key.endswith('_values') else 145)
        stats_layout.addWidget(self.stats_table, 1)
        self.export_button = QtWidgets.QPushButton(self.t('导出统计 CSV…', 'Export statistics CSV…'))
        self.export_button.clicked.connect(self.export_statistics)
        stats_layout.addWidget(self.export_button)
        self.tabs.addTab(stats_page, self.t('范围统计', 'Range statistics'))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.button(QtWidgets.QDialogButtonBox.Close).setText(self.t('关闭', 'Close'))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.range_mode.currentIndexChanged.connect(self.reset_range)
        self.start_input.valueChanged.connect(self.update_statistics)
        self.end_input.valueChanged.connect(self.update_statistics)
        self.signal_mode.currentIndexChanged.connect(self.update_statistics)
        self.reset_range()

    def t(self, zh, en):
        return zh if self.zh else en

    def open_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, self.t('读取往期日志', 'Open historical logs'),
            str(Path(__file__).resolve().parent/'logs'), 'Logs (*.jsonl *.csv);;JSONL (*.jsonl);;CSV (*.csv)')
        if not paths:
            return False
        try:
            self.load_paths(paths)
            return True
        except (OSError, ValueError, csv.Error, UnicodeError) as exc:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), str(exc))
            return False

    def load_paths(self, paths):
        configurations = []
        records, counts, issues = read_log_files(paths, self.decode, self.phy_passed, configurations)
        self.model.beginResetModel()
        self.records, self.sources, self.issues = records, [str(path) for path in paths], issues
        self.model.endResetModel()
        self.configurations = configurations
        self.config_button.setEnabled(bool(configurations))
        self.source_label.setText(', '.join(Path(path).name for path in paths))
        self.source_label.setToolTip('\n'.join(str(path) for path in paths))
        self.load_summary.setText(self.t(
            f"读取 {counts.get('loaded', 0)} 条；LB 成功 {counts.get('lb_ok', 0)}，失败 {counts.get('lb_rejected', 0)}；"
            f"丢弃 PHY 未通过 {counts.get('phy_dropped', 0)} 条、损坏行 {counts.get('malformed', 0)} 条；配置记录 {len(configurations)} 条。",
            f"Loaded {counts.get('loaded', 0)}; LB valid {counts.get('lb_ok', 0)}, rejected {counts.get('lb_rejected', 0)}; "
            f"dropped PHY-unverified {counts.get('phy_dropped', 0)}, malformed {counts.get('malformed', 0)}; configurations {len(configurations)}."))
        self.load_summary.setToolTip('\n'.join(issues[:30]))
        self.reset_range()

    def show_configurations(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t('日志中的接收端配置', 'Receiver configurations in log'))
        dialog.resize(850, 650)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setFont(QtGui.QFont('Consolas', 10))
        text.setPlainText('\n\n'.join(json.dumps(config, ensure_ascii=False, indent=2) for config in self.configurations))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.button(QtWidgets.QDialogButtonBox.Close).setText(self.t('关闭', 'Close'))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()

    def reset_range(self):
        blockers = [QtCore.QSignalBlocker(self.start_input), QtCore.QSignalBlocker(self.end_input)]
        sequence = self.range_mode.currentData() == 'sequence'
        for widget in (self.start_input, self.end_input):
            widget.setRange(0 if sequence else 1, 255 if sequence else max(1, len(self.records)))
        self.start_input.setValue(0 if sequence else 1)
        self.end_input.setValue(255 if sequence else max(1, len(self.records)))
        del blockers
        self.update_statistics()

    def update_statistics(self):
        try:
            self.report = range_statistics(self.records, self.start_input.value(), self.end_input.value(),
                                           self.range_mode.currentData(), self.signal_mode.currentData())
        except ValueError:
            self.report = None
            self.stats_table.setRowCount(0)
            self.stats_summary.setText(self.t('日志行号起点不能大于终点。', 'Row start must not exceed row end.'))
            self.export_button.setEnabled(False)
            return
        report = self.report
        self.stats_summary.setText(self.t(
            f"总设备数：{report['devices']}；有效包数：{report['packets']}；范围内行数：{report['selected_rows']}；"
            f"排除无效行：{report['excluded_rows']}。",
            f"Devices: {report['devices']}; valid packets: {report['packets']}; rows in range: {report['selected_rows']}; "
            f"excluded invalid rows: {report['excluded_rows']}."))
        self.stats_table.setRowCount(len(report['rows']))
        for row, device in enumerate(report['rows']):
            for column, (key, _, _) in enumerate(STATS_COLUMNS):
                value = device[key]
                text = '—' if value is None else f'{value:.2f}' if isinstance(value, float) else str(value)
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(text)
                item.setBackground(self.device_color(int(device['device_id'], 16)))
                self.stats_table.setItem(row, column, item)
        self.export_button.setEnabled(bool(report['rows']))

    def export_statistics(self):
        if not self.report or not self.report['rows']:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t('导出统计', 'Export statistics'),
                                                      'statistics.csv', 'CSV (*.csv)')
        if not path:
            return
        try:
            self.write_statistics(path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), str(exc))

    def write_statistics(self, path):
        metadata = {key: self.report[key] for key in ('range_mode', 'range_start', 'range_end', 'signal_field',
                                                     'selected_rows', 'excluded_rows')}
        metadata.update(total_devices=self.report['devices'], total_packets=self.report['packets'])
        metadata['source_files'] = json.dumps(self.sources, ensure_ascii=False) if self.sources else 'current_session_snapshot'
        metadata['receiver_configurations_json'] = json.dumps(self.configurations, ensure_ascii=False)
        with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(metadata)+[key for key, _, _ in STATS_COLUMNS])
            writer.writeheader()
            for row in self.report['rows']:
                flat = dict(metadata, **{key: value for key, value in row.items() if key in writer.fieldnames})
                writer.writerow(flat)
