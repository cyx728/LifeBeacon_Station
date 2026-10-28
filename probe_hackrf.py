"""Read-only finite HackRF IQ acquisition; never opens a transmitter."""
import json
import numpy as np
from gnuradio import gr, blocks
import osmosdr
from station import discover_devices, core

device = next(d for d, name in discover_devices() if d.startswith('hackrf='))
tb = gr.top_block()
source = osmosdr.source(args='numchan=1 '+device)
source.set_sample_rate(2400000)
source.set_center_freq(433375000)
source.set_gain(0, 'RF'); source.set_gain(16, 'IF'); source.set_gain(20, 'BB')
assert source.get_gain('RF') == 0 and source.get_gain('IF') == 16 and source.get_gain('BB') == 20
head = blocks.head(gr.sizeof_gr_complex, 240000)
sink = blocks.vector_sink_c()
tb.connect(source, head, sink)
tb.run()
iq = np.asarray(sink.data(), dtype=np.complex64)
assert len(iq) == 240000 and np.isfinite(iq).all() and np.max(np.abs(iq)) > 0
report = dict(test='live receive only, no RF transmitted', device=device, samples=len(iq),
              sample_rate=source.get_sample_rate(), frequency=source.get_center_freq(),
              gain_names=list(source.get_gain_names()),
              gains={name: source.get_gain(name) for name in source.get_gain_names()},
              average_dbfs=float(10*np.log10(np.mean(np.abs(iq)**2))))
(core.ROOT/'hackrf-rx-test.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
