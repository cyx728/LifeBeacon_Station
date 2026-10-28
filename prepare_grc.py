"""Generate the RTL-SDR flowgraph from the installed EPFL LoRa example."""
from pathlib import Path
import yaml

root = Path(__file__).resolve().parent
radio = Path('D:/ProgramData/radioconda/Library/share/gnuradio')
graph = yaml.safe_load((radio/'examples/lora_sdr/lora_RX.grc').read_text())
graph['options']['parameters'].update(id='RTL_SDR_LoRa_RX', title='RTL-SDR LoRa 433.775 MHz',
                                    author='LifeBeacon / EPFL gr-lora_sdr', generate_options='no_gui')
graph['blocks'] = [b for b in graph['blocks'] if b['id'] != 'uhd_usrp_source']
for b in graph['blocks']:
    if b['name'] == 'center_freq':
        b['parameters']['value'] = '433775000'
    if b['name'] == 'pay_len':
        b['parameters']['value'] = '255'
    if b['id'] == 'lora_sdr_crc_verif':
        b['parameters']['print_rx_msg'] = '2'

def add(name, block_id, params, coord):
    graph['blocks'].append(dict(name=name, id=block_id, parameters=params,
        states=dict(coordinate=coord, rotation=0, state='enabled', bus_sink=False,
                    bus_source=False, bus_structure=None)))

source_def = yaml.safe_load((radio/'grc/blocks/osmosdr_source.block.yml').read_text())
params = {p['id']: str(p.get('default', '')) for p in source_def['parameters']}
params.update(args='rtl=0', numchan='1', nchan='1', sample_rate='1000000',
              freq0='center_freq', corr0='0', gain0='28', gain_mode0='False',
              bandwidth0='0', type='fc32')
add('rtl_source', 'osmosdr_source', params, [16, 180])
add('resample', 'rational_resampler_xxx', dict(type='ccf', interp='1', decim='2',
    taps='[]', fbw='0.4'), [200, 180])
graph['connections'] = [c for c in graph['connections'] if c[0] != 'uhd_usrp_source_0']
graph['connections'] += [['rtl_source', '0', 'resample', '0'],
                         ['resample', '0', 'lora_sdr_frame_sync_0', '0']]
(root/'RTL_SDR_LoRa_RX.grc').write_text(yaml.safe_dump(graph, sort_keys=False), encoding='utf-8')
print(root/'RTL_SDR_LoRa_RX.grc')
