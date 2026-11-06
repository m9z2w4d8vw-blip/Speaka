"""Bit-exact LATM / LOAS / ADTS / RFC 3640 packet writers (ISO/IEC 14496-3 1.7.3, RFC 3640) for the AAC tests.

Validated independently: ffmpeg's LOAS/LATM decoder decodes every single-subframe variant this writes (config in
every element, config once then useSameStreamMux, audioMuxVersion 1 with fill bits, otherData with CRC). It is
not a test module (unittest discovery only loads test*.py)."""
class W:
    def __init__(self): self.bits = []
    def put(self, v, n):
        for i in range(n - 1, -1, -1): self.bits.append((v >> i) & 1)
    def align(self):
        while len(self.bits) % 8: self.bits.append(0)
    def data(self):
        self.align(); out = bytearray()
        for i in range(0, len(self.bits), 8):
            b = 0
            for j in range(8): b = (b << 1) | self.bits[i + j]
            out.append(b)
        return bytes(out)

def latm_get_value(w, v):
    n = 0 if v < 1 << 8 else 1 if v < 1 << 16 else 2 if v < 1 << 24 else 3
    w.put(n, 2)
    for i in range(n, -1, -1): w.put((v >> (8 * i)) & 0xff, 8)

def stream_mux_config(w, version=0, subframes=0, other_bits=None, crc=None, asc=0x1190, asc_fill=0):
    w.put(version, 1)
    if version == 1:
        w.put(0, 1)                      # audioMuxVersionA
        latm_get_value(w, 0xff)          # taraBufferFullness
    w.put(1, 1); w.put(subframes, 6); w.put(0, 4); w.put(0, 3)
    if version == 0:
        w.put(asc, 16)
    else:
        latm_get_value(w, 16 + asc_fill); w.put(asc, 16); w.put(0, asc_fill)
    w.put(0, 3); w.put(0xff, 8)          # frameLengthType 0, latmBufferFullness
    if other_bits is None:
        w.put(0, 1)
    else:
        w.put(1, 1)
        if version == 1:
            latm_get_value(w, other_bits)
        else:                            # escaped 8-bit chunks, most significant first
            chunks = []
            v = other_bits
            while True:
                chunks.insert(0, v & 0xff); v >>= 8
                if not v: break
            for i, c in enumerate(chunks): w.put(1 if i < len(chunks) - 1 else 0, 1); w.put(c, 8)
    if crc is None: w.put(0, 1)
    else: w.put(1, 1); w.put(crc, 8)

def element(aus, config=None, mux_config_present=True, other_bits=0):
    """config: dict for stream_mux_config (useSameStreamMux=0), or None (useSameStreamMux=1)."""
    w = W()
    if mux_config_present:
        if config is not None: w.put(0, 1); stream_mux_config(w, **config)
        else: w.put(1, 1)
    for au in aus:
        n = len(au)
        while n >= 255: w.put(255, 8); n -= 255
        w.put(n, 8)
        for b in au: w.put(b, 8)
    if other_bits: w.put(0, other_bits)
    return w.data()

def loas(elem):
    n = len(elem); return bytes([0x56, 0xe0 | (n >> 8), n & 0xff]) + elem

def adts(au, freq_index=3, channels=2, crc=False):
    hdr = 9 if crc else 7; total = hdr + len(au)
    h = bytes([0xff, 0xf0 | (0 if crc else 1), (1 << 6) | (freq_index << 2) | (channels >> 2),
               ((channels & 3) << 6) | (total >> 11), (total >> 3) & 0xff, ((total & 7) << 5) | 0x1f, 0xfc])
    return h + (b"\x00\x00" if crc else b"") + au

def rfc3640(aus):
    head = bytes([(16 * len(aus)) >> 8, (16 * len(aus)) & 0xff])
    for au in aus: head += bytes([(len(au) << 3) >> 8, (len(au) << 3) & 0xff])
    return head + b"".join(aus)
