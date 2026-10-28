"""Check strict and wildcard sync recovery on an alternate LoRa sync word."""
import binascii
import math

import receiver as rx


def make_iq(sync_word):
    name = b'TEST'
    raw = b'LB\x01\x01\x01' + (0x10203040).to_bytes(4, 'little') + bytes([len(name)]) + name
    raw += bytes([3, 72, 98, 0, 0, 85, 9, 128, 0])
    raw += binascii.crc_hqx(raw, 0xffff).to_bytes(2, 'little')
    generator = rx.gr.top_block()
    payload = rx.blocks.vector_source_b(list(raw), False)
    tagged = rx.blocks.stream_to_tagged_stream(rx.gr.sizeof_char, 1, len(raw), 'packet_len')
    whitening = rx.lora_sdr.whitening(False, True, ',', 'packet_len')
    header = rx.lora_sdr.header(False, True, 1)
    crc = rx.lora_sdr.add_crc(True)
    hamming = rx.lora_sdr.hamming_enc(1, 7)
    interleave = rx.lora_sdr.interleaver(1, 7, 2, 125000)
    gray = rx.lora_sdr.gray_demap(7)
    modulate = rx.lora_sdr.modulate(7, 1000000, 125000, [sync_word], 10000, 8)
    sink = rx.blocks.vector_sink_c()
    generator.connect(payload, tagged, whitening, header, crc, hamming, interleave, gray, modulate, sink)
    generator.run()
    return raw, rx.np.concatenate([rx.np.zeros(4096, rx.np.complex64),
                                   rx.np.asarray(sink.data(), rx.np.complex64),
                                   rx.np.zeros(15000, rx.np.complex64)])


raw, iq = make_iq(0x34)
strict_records = []
strict = rx.Receiver(dict(frequency=433775000, sample_rate=1000000, bw=125000,
                          sf=7, sync=0x12, sync_auto=False), strict_records.append,
                     False, rx.blocks.vector_source_c(iq.tolist(), False))
strict.run()
assert not [record for record in strict_records if record.get('raw_payload_hex') == raw.hex(' ').upper()]

wildcard_records = []
wildcard = rx.Receiver(dict(frequency=433775000, sample_rate=1000000, bw=125000,
                            sf=7, sync=0x12, sync_auto=True), wildcard_records.append,
                       False, rx.blocks.vector_source_c(iq.tolist(), False))
wildcard.run()
valid = [record for record in wildcard_records
         if record.get('raw_payload_hex') == raw.hex(' ').upper()
         and record.get('status') == 'PHY CRC OK / LB OK']
assert len(valid) == 1, wildcard_records
assert rx.decoder_sync_word(dict(sync=0x12, sync_auto=False)) == [0x12]
assert rx.decoder_sync_word(dict(sync=0x12, sync_auto=True)) == [0, 0]
print('PASS: strict sync rejects alternate word; wildcard recovery decodes it with PHY/LB CRC')
