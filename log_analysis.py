"""Read-only log import and range statistics; no radio or GUI dependencies."""
import csv
import json
import math
from collections import Counter
from pathlib import Path


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def read_log_files(paths, decode, phy_passed, metadata=None):
    """Recheck every imported payload; never rewrite sources or trust cached fields."""
    records, counts, issues = [], Counter(), []
    numeric_keys = ('signal_avg_dbfs', 'signal_peak_dbfs', 'signal_samples',
                    'receiver_gain_db_at_display', 'channel_frequency_hz', 'spreading_factor', 'lora_bandwidth_hz')

    def accept(record, path, line):
        if isinstance(record, dict) and record.get('record_type') == 'receiver_config':
            config = record.get('receiver_config')
            if config is None and record.get('receiver_config_json'):
                config = json.loads(record['receiver_config_json'])
            config_id = record.get('config_id') or record.get('receiver_config_id')
            if not isinstance(config, dict) or not isinstance(config_id, str) or not config_id:
                raise ValueError('Invalid receiver configuration header')
            if metadata is not None:
                metadata.append(dict(record_type='receiver_config', config_id=config_id,
                    time=record.get('time'), event=record.get('event', record.get('config_event')),
                    receiver_config=config, _source_file=str(path), _source_line=line))
            counts['config_headers'] += 1
            return
        counts['input_rows'] += 1
        if not isinstance(record, dict) or not isinstance(record.get('raw_payload_hex'), str):
            raise ValueError('Missing raw_payload_hex')
        bytes.fromhex(record['raw_payload_hex'])
        record = dict(record)
        # CSV exports from older versions indicate PHY validity via status only.
        if 'phy_crc_valid' in record:
            flag = record['phy_crc_valid']
            if isinstance(flag, str):
                flag = flag.strip().lower()
                if flag not in ('true', 'false', '1', '0'):
                    raise ValueError('Invalid phy_crc_valid')
                flag = flag in ('true', '1')
            if not isinstance(flag, bool):
                raise ValueError('Invalid phy_crc_valid')
            record['phy_crc_valid'] = flag
        if not isinstance(record.get('status', ''), str):
            raise ValueError('Invalid status')
        if not phy_passed(record):
            counts['phy_dropped'] += 1
            return
        for key in numeric_keys:
            if key in record:
                record[key] = finite_number(record[key])
        record = decode(record)
        record['time'] = str(record.get('time') or '—')
        record['_source_file'] = str(path)
        record['_source_line'] = line
        records.append(record)
        counts['loaded'] += 1
        counts['lb_ok' if record.get('fields') else 'lb_rejected'] += 1

    for source in dict.fromkeys(str(Path(p).resolve()) for p in paths):
        path = Path(source)
        with path.open(encoding='utf-8-sig', newline='') as stream:
            if path.suffix.lower() == '.csv':
                reader = csv.DictReader(stream)
                if not reader.fieldnames or 'raw_payload_hex' not in reader.fieldnames:
                    raise ValueError(f'{path.name}: missing raw_payload_hex CSV column')
                for record in reader:
                    try:
                        accept(record, path, reader.line_num)
                    except (ValueError, TypeError, KeyError) as exc:
                        counts['malformed'] += 1
                        issues.append(f'{path.name}:{reader.line_num}: {exc}')
            else:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError as exc:
                        counts['input_rows'] += 1
                        counts['malformed'] += 1
                        issues.append(f'{path.name}:{line_number}: {exc}')
                        continue
                    try:
                        accept(record, path, line_number)
                    except (ValueError, TypeError, KeyError) as exc:
                        counts['malformed'] += 1
                        issues.append(f'{path.name}:{line_number}: {exc}')
    return records, dict(counts), issues


def range_statistics(records, start, end, mode='row', signal='signal_avg_dbfs'):
    if mode not in ('row', 'sequence') or signal not in ('signal_avg_dbfs', 'signal_peak_dbfs'):
        raise ValueError('Invalid range or signal mode')
    if mode == 'row' and (start < 1 or end < start):
        raise ValueError('Row range must be ascending and start at 1')
    if mode == 'sequence' and not (0 <= start <= 255 and 0 <= end <= 255):
        raise ValueError('Sequence must be between 0 and 255')
    selected = []
    for index, record in enumerate(records, 1):
        if mode == 'row':
            include = start <= index <= end
        else:
            value = (record.get('fields') or {}).get('sequence')
            include = value is not None and (start <= value <= end if start <= end else value >= start or value <= end)
        if include:
            selected.append(record)
    devices = {}
    for record in selected:
        fields = record.get('fields') or {}
        if not fields or not record.get('phy_crc_valid', record.get('status', '').startswith('PHY CRC OK')):
            continue
        device = fields['device_id']
        group = devices.setdefault(device, dict(device_id=f'0x{device:08X}', name='', packets=0,
                                                _signal=[], _hr=[], _spo2=[]))
        group['name'] = fields.get('name') or group['name']
        group['packets'] += 1
        value = finite_number(record.get(signal))
        if value is not None:
            group['_signal'].append(value)
        for key, bit in (('hr', 1), ('spo2', 2)):
            value = finite_number(fields.get(key))
            if fields.get('physiology_flags', 0) & bit and value is not None and value != 255:
                group['_'+key].append(value)
    rows = []
    for device in sorted(devices):
        group = devices[device]
        for key in ('signal', 'hr', 'spo2'):
            values = group.pop('_'+key)
            group[key+'_valid'] = len(values)
            group[key+'_min'] = min(values) if values else None
            group[key+'_max'] = max(values) if values else None
            group[key+'_avg'] = sum(values)/len(values) if values else None
            if key != 'signal':
                group[key+'_valid_percent'] = len(values)*100/group['packets']
                group[key+'_values'] = ', '.join(f'{value:g}' for value in values)
        rows.append(group)
    return dict(selected_rows=len(selected), packets=sum(row['packets'] for row in rows),
                excluded_rows=len(selected)-sum(row['packets'] for row in rows), devices=len(rows), rows=rows,
                range_mode=mode, range_start=start, range_end=end, signal_field=signal)
