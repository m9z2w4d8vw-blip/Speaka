"""AAC-LC support (experimental).

Run on every host:
  - the A2DP AAC capability and configuration, the MPEG-4 AudioSpecificConfig, the ESDS magic cookie (checked
    against the one ffmpeg's MP4 muxer writes);
  - LATM depacketization against ffmpeg's own output, including truncated elements;
  - the decoder's contract with AudioConverter, through tests/fakes (one bare access unit per packet, a packet
    description, the software class description, the cookie), fragment reassembly and in-band config changes;
  - the codec dispatch, which never decodes AAC as SBC;
  - the on-device self-test (BTPSAudioHelper --aac-selftest): stages, markers, JSON.

Run only where the real AudioToolbox exists, in CI's device-test job (SPEAKA_REAL_AAC=1 on macOS):
  - RealAudioToolbox: Apple's actual AAC decoder decodes the fixture, and the PCM matches ffmpeg's decode of it.
The phone runs the same decoder through the self-test (tools/device_test).
"""
import ctypes
import random
import re
import importlib.util
import json
import math
import os
import shutil
import sys
import pathlib
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import aac_packets  # noqa: E402  (bit-exact packet writers, validated against ffmpeg)


def build_lib(tmp, *sources, extra=()):
    lib = pathlib.Path(tmp) / "aac.so"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    "-o", str(lib), *[str(ROOT / s) for s in sources], *extra], check=True)
    return ctypes.CDLL(str(lib))


class AACConfigParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = build_lib(cls.tmp.name, "shared/AACConfig.c")
        cls.lib.aac_build_sink_capability.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        cls.lib.aac_build_sink_capability.restype = ctypes.c_size_t
        cls.lib.aac_build_asc.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8)]
        cls.lib.aac_build_asc.restype = ctypes.c_size_t
        cls.lib.aac_sampling_frequency_index.argtypes = [ctypes.c_uint32]
        cls.lib.aac_sampling_frequency_index.restype = ctypes.c_uint

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # AACConfig mirrors the C struct: object_type u8, sample_rate u32, channels u8, vbr u8, bitrate u32.
    class AACConfig(ctypes.Structure):
        _fields_ = [("object_type", ctypes.c_uint8), ("sample_rate", ctypes.c_uint32),
                    ("channels", ctypes.c_uint8), ("vbr", ctypes.c_uint8), ("bitrate", ctypes.c_uint32)]

    def parse(self, payload):
        cfg = self.AACConfig()
        csi = (ctypes.c_uint8 * 6)(*payload)
        ok = self.lib.aac_parse_config
        ok.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(self.AACConfig)]
        ok.restype = ctypes.c_int
        return ok(csi, ctypes.byref(cfg)), cfg

    def supported(self, cfg):
        fn = self.lib.aac_config_supported
        fn.argtypes = [ctypes.POINTER(self.AACConfig)]
        fn.restype = ctypes.c_int
        return fn(ctypes.byref(cfg))

    def test_sink_capability_offers_lc_441_and_48_stereo(self):
        out = (ctypes.c_uint8 * 6)()
        self.assertEqual(self.lib.aac_build_sink_capability(out), 6)
        # Object type byte: both MPEG-2 LC (0x80) and MPEG-4 LC (0x40).
        self.assertEqual(out[0], 0x80 | 0x40)
        freq = (out[1] << 4) | (out[2] >> 4)
        self.assertEqual(freq, 0x0010 | 0x0008)              # 44100 | 48000
        self.assertEqual(out[2] & 0x0f, 0x08 | 0x04)         # mono | stereo
        self.assertEqual(out[3] & 0x80, 0x80)                # VBR allowed

    def test_parse_rejects_a_capability_range_and_accepts_one_choice(self):
        # The Sink capability (a range: two rates, two channel modes) is not a valid CONFIGURE.
        ok, _ = self.parse([0xC0, 0x01, 0x8C, 0x80, 0, 0])
        self.assertEqual(ok, 0)
        # A chosen config: MPEG-4 LC, 48 kHz, stereo.
        ok, cfg = self.parse([0x40, 0x00, 0x84, 0x80, 0, 0])
        self.assertEqual(ok, 1)
        self.assertEqual((cfg.object_type, cfg.sample_rate, cfg.channels), (0x40, 48000, 2))
        self.assertEqual(self.supported(cfg), 1)

    def test_parse_44100_mono_and_bitrate(self):
        # MPEG-2 LC, 44.1 kHz, mono, VBR, bitrate 0x00000 (unspecified).
        ok, cfg = self.parse([0x80, 0x01, 0x08, 0x80, 0x00, 0x00])
        self.assertEqual(ok, 1)
        self.assertEqual((cfg.object_type, cfg.sample_rate, cfg.channels, cfg.vbr), (0x80, 44100, 1, 1))
        # A constant bitrate value survives.
        ok, cfg = self.parse([0x40, 0x00, 0x84, 0x02, 0x80, 0x00])
        self.assertEqual(ok, 1)
        self.assertEqual(cfg.bitrate, (0x02 << 16) | (0x80 << 8))

    def test_unsupported_rate_is_parsed_but_not_supported(self):
        # 96 kHz LC stereo: a valid single choice, but the helper's decoder only supports 44.1/48.
        ok, cfg = self.parse([0x40, 0x00, 0x14, 0x80, 0, 0])
        self.assertEqual((ok, cfg.sample_rate), (1, 96000))
        self.assertEqual(self.supported(cfg), 0)

    def test_asc_matches_mpeg4_layout(self):
        # 48 kHz stereo: object 2 (5b) | freq index 3 (4b) | channels 2 (4b) | 000.
        _, cfg = self.parse([0x40, 0x00, 0x84, 0x80, 0, 0])
        out = (ctypes.c_uint8 * 2)()
        self.assertEqual(self.lib.aac_build_asc(ctypes.byref(cfg), out), 2)
        asc = (out[0] << 8) | out[1]
        self.assertEqual((asc >> 11) & 0x1f, 2)              # AAC-LC
        self.assertEqual((asc >> 7) & 0x0f, 3)               # 48000 index
        self.assertEqual((asc >> 3) & 0x0f, 2)               # stereo
        self.assertEqual(self.lib.aac_sampling_frequency_index(44100), 4)
        self.assertEqual(self.lib.aac_sampling_frequency_index(48000), 3)
        self.assertEqual(self.lib.aac_sampling_frequency_index(11111), 0xf)


class AACCapabilityRecord(unittest.TestCase):
    def test_tweak_record_header_then_csi(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = build_lib(tmp, "tweak/SinkEndpoint.c", "shared/AACConfig.c")
            lib.sink_build_aac_codec_record.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            lib.sink_build_aac_codec_record.restype = ctypes.c_size_t
            out = (ctypes.c_uint8 * 8)()
            self.assertEqual(lib.sink_build_aac_codec_record(out), 8)
            self.assertEqual(out[0], 0x00)                   # media type audio
            self.assertEqual(out[1], 0x02)                   # codec type AAC
            self.assertEqual(out[2], 0xC0)                   # same CSI the capability builder makes



FAKES = ROOT / "tests/fakes"
DECODER_SOURCES = ["audiohelper/AACDecoder.c", "shared/AACPacket.c", "shared/LATM.c", "shared/AACConfig.c"]
IS_MAC = sys.platform == "darwin"


def compile_c(tmp, name, sources, *, real_audiotoolbox=False, shared=False, extra_includes=(), lenient=()):
    """Build an executable or shared library from repo-relative sources. With real_audiotoolbox the real framework is
    used (macOS only); otherwise tests/fakes stands in for it and is compiled in. `lenient` sources (upstream code)
    build without -Werror; ours always build with it."""
    out = pathlib.Path(tmp) / name
    includes = ["-I", str(ROOT / "audiohelper"), "-I", str(ROOT)]
    for inc in extra_includes:
        includes += ["-I", str(inc)]
    objects = []
    for src in lenient:
        obj = pathlib.Path(tmp) / (pathlib.Path(src).stem + ".lenient.o")
        r = subprocess.run(["cc", "-std=gnu11", "-fPIC", "-O1", *includes, "-c", str(ROOT / src), "-o", str(obj)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError("compile failed:\n" + r.stderr)
        objects.append(str(obj))
    cmd = ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", *includes]
    srcs = [str(ROOT / s) for s in sources] + objects
    if real_audiotoolbox:
        link = ["-framework", "AudioToolbox"]
    else:
        cmd += ["-I", str(FAKES)]
        srcs.append(str(FAKES / "fake_audiotoolbox.c"))
        link = [] if IS_MAC else ["-lm"]
    if shared:
        cmd += ["-shared", "-fPIC"]
    r = subprocess.run(cmd + srcs + ["-o", str(out)] + link, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("compile failed:\n" + r.stderr)
    return out


def fixture_elements():
    """The ffmpeg LATM fixture as bare AudioMuxElements (LOAS framing stripped), and ffmpeg's ADTS access units."""
    loas = (ROOT / "tests/fixtures/aac_latm_48k_stereo.loas").read_bytes()
    elements, i = [], 0
    while i + 3 <= len(loas):
        word = (loas[i] << 8) | loas[i + 1]
        if (word >> 5) != 0x2B7:
            break
        length = ((word & 0x1f) << 8) | loas[i + 2]
        elements.append(loas[i + 3:i + 3 + length])
        i += 3 + length
    data = (ROOT / "tests/fixtures/aac_latm_48k_stereo.aus").read_bytes()
    count, off, aus = struct.unpack_from("<I", data, 0)[0], 4, []
    for _ in range(count):
        length = struct.unpack_from("<I", data, off)[0]
        aus.append(data[off + 4:off + 4 + length])
        off += 4 + length
    return elements, aus


class CreateStatus(ctypes.Structure):
    _fields_ = [("specific_status", ctypes.c_int32), ("new_status", ctypes.c_int32), ("cookie_status", ctypes.c_int32),
                ("used_software", ctypes.c_int), ("cookie_set", ctypes.c_int)]


class Decoder:
    """AACDecoder through tests/aac_decoder_driver.c."""
    RAW, ADTS = 0, 1
    ESDS, ASC, NONE = 0, 1, 2

    def __init__(self, lib):
        self.lib = lib
        lib.drv_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                   ctypes.POINTER(CreateStatus)]
        lib.drv_create.restype = ctypes.c_void_p
        lib.drv_decode.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_char_p,
                                   ctypes.c_uint, ctypes.POINTER(ctypes.c_int16), ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        lib.drv_decode.restype = ctypes.c_int
        lib.aac_decoder_destroy.argtypes = [ctypes.c_void_p]
        for fn in ("aac_decoder_last_status", "aac_decoder_config_changes", "aac_decoder_fragments_joined",
                   "aac_decoder_fragments_dropped"):
            getattr(lib, fn).argtypes = [ctypes.c_void_p]
        lib.aac_decoder_last_status.restype = ctypes.c_int32
        assert lib.drv_create_status_size() == ctypes.sizeof(CreateStatus), "CreateStatus mirror is out of date"
        self.handle = None

    def create(self, framing=-1, cookie=-1, prefer_software=-1, rate=48000, channels=2):
        self.status = CreateStatus()
        self.handle = self.lib.drv_create(framing, cookie, prefer_software, rate, channels, ctypes.byref(self.status))
        return self.handle

    def decode(self, data, sequence, timestamp, generation=1):
        pcm = (ctypes.c_int16 * (16 * 2048))()             # several AAC frames per packet
        samples, rate = ctypes.c_uint(), ctypes.c_uint()
        r = self.lib.drv_decode(self.handle, generation, sequence, timestamp, bytes(data), len(data), pcm, 16 * 2048,
                                ctypes.byref(samples), ctypes.byref(rate))
        return r, samples.value, rate.value, pcm

    def stat(self, name):
        return getattr(self.lib, "aac_decoder_" + name)(self.handle)

    def destroy(self):
        if self.handle:
            self.lib.aac_decoder_destroy(self.handle)
            self.handle = None



class ESDSAndASC(unittest.TestCase):
    """The ESDS magic cookie against the esds box ffmpeg's MP4 muxer writes for the same stream, and the ASC reader."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = build_lib(cls.tmp.name, "shared/AACConfig.c")
        cls.lib.aac_build_esds.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                                           ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        cls.lib.aac_build_esds.restype = ctypes.c_size_t
        cls.lib.aac_config_from_asc.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                                ctypes.POINTER(AACConfigParsing.AACConfig)]
        cls.lib.aac_config_from_asc.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def esds(self, asc, bitrate=0, cap=64):
        out = (ctypes.c_uint8 * 64)()
        n = self.lib.aac_build_esds(asc, len(asc), bitrate, out, cap)
        return bytes(out[:n])

    def test_esds_matches_ffmpeg_mp4_muxer(self):
        mine = self.esds(bytes([0x11, 0x90]))
        ffmpeg = (ROOT / "tests/fixtures/aac_esds_ffmpeg.bin").read_bytes()
        self.assertEqual(len(mine), len(ffmpeg))
        # Bytes 5-6: ES_ID, the MP4 track number in ffmpeg's box (0 in a cookie). Bytes 15-25: buffer size and
        # bitrates, which ffmpeg measured from the stream. Everything else is the layout and must be identical.
        masked = set(range(5, 7)) | set(range(15, 26))
        for k, (a, b) in enumerate(zip(mine, ffmpeg)):
            if k not in masked:
                self.assertEqual(a, b, f"byte {k}")
        self.assertEqual(mine[26:33], bytes([0x05, 0x80, 0x80, 0x80, 0x02, 0x11, 0x90]))   # the ASC inside

    def test_esds_bitrate_and_limits(self):
        cookie = self.esds(bytes([0x11, 0x90]), bitrate=256000)
        self.assertEqual(struct.unpack(">II", cookie[18:26]), (256000, 256000))
        self.assertEqual(self.esds(bytes([0x11, 0x90]), cap=38), b"")
        self.assertEqual(self.esds(b""), b"")

    def test_config_from_asc(self):
        cfg = AACConfigParsing.AACConfig()
        self.assertEqual(self.lib.aac_config_from_asc(bytes([0x11, 0x90]), 2, ctypes.byref(cfg)), 1)
        self.assertEqual((cfg.sample_rate, cfg.channels), (48000, 2))
        self.assertEqual(self.lib.aac_config_from_asc(bytes([0x12, 0x08]), 2, ctypes.byref(cfg)), 1)
        self.assertEqual((cfg.sample_rate, cfg.channels), (44100, 1))
        for bad in ([0x10, 0x10],    # 96 kHz
                    [0x11, 0x94],    # 960-sample frames
                    [0x29, 0x90],    # object type 5 (SBR), not AAC-LC
                    [0x11]):         # too short
            self.assertEqual(self.lib.aac_config_from_asc(bytes(bad), len(bad), ctypes.byref(cfg)), 0, bad)


class FixtureHeaderInSync(unittest.TestCase):
    def test_committed_header_matches_the_fixture(self):
        spec = importlib.util.spec_from_file_location("make_aac_fixture_header",
                                                      ROOT / "tools/make_aac_fixture_header.py")
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        self.assertEqual((ROOT / "audiohelper/AACSelfTestFixture.h").read_text(), gen.render(),
                         "the fixture changed: run python3 tools/make_aac_fixture_header.py")


class CodecDispatchFallback(unittest.TestCase):
    """codec_decoder_begin: SBC sessions use SBC; an AAC session it can't decode goes DISABLED (silent), never SBC.
    Compiles the real SBCDecoder.c with the real vendored libsbc; tests/fakes stands in for AudioToolbox."""
    @classmethod
    def setUpClass(cls):
        vendor = ROOT / "audiohelper/vendor_sbc"
        if not (vendor / "sbc.c").exists() or not (vendor / "bits.c").exists():
            if os.environ.get("CI"):
                raise AssertionError("vendored libsbc missing in CI; the fetch step must run before the tests")
            raise unittest.SkipTest("vendored libsbc not present; run tools/fetch_google_sbc.sh (CI does)")
        cls.tmp = tempfile.TemporaryDirectory()
        shim = pathlib.Path(cls.tmp.name) / "shim.c"
        shim.write_text(r"""
            #include "CodecDecoder.h"
            unsigned begin_codec(unsigned codec, const unsigned char *csi, int aac_enabled) {
                AudioRing h; h.codec = codec;
                for (int i = 0; i < 8; i++) h.codec_config[i] = csi[i];
                CodecDecoder d; unsigned used = codec_decoder_begin(&d, &h, aac_enabled, 0);
                codec_decoder_end(&d); return used;
            }
            #include <stdarg.h>
            #include <stdio.h>
            #include <string.h>
            static char *g_out; static unsigned g_cap, g_used; static int g_lines;
            static void capture(const char *fmt, ...) {
                va_list ap; va_start(ap, fmt);
                int n = vsnprintf(g_out + g_used, g_cap - g_used, fmt, ap);
                va_end(ap);
                if (n > 0 && g_used + (unsigned)n < g_cap) { g_used += (unsigned)n; g_lines++; }
            }
            int log_packets(const unsigned char *flat, unsigned count, char *out, unsigned cap) {
                static AudioRing h; static AudioPacket p; static int16_t pcm[16 * 2048];
                const unsigned char aac[8] = {0x40, 0x00, 0x84, 0x80, 0, 0, 0, 0};   /* LC 48 kHz stereo */
                g_out = out; g_cap = cap; g_used = 0; g_lines = 0; out[0] = 0;
                memset(&h, 0, sizeof h); h.codec = 2; memcpy(h.codec_config, aac, 8);
                CodecDecoder d; codec_decoder_begin(&d, &h, 1, capture);
                unsigned off = 0, ts = 0;
                for (unsigned i = 0; i < count; i++) {
                    unsigned len; memcpy(&len, flat + off, 4); off += 4;
                    memset(&p, 0, sizeof p); p.generation = 1; p.sequence = (uint16_t)i; p.timestamp = ts;
                    p.length = len; memcpy(p.data, flat + off, len); off += len; ts += 1024;
                    unsigned frames = 0, samples = 0, rate = 0;
                    codec_decoder_decode(&d, &p, pcm, sizeof pcm / sizeof pcm[0], &frames, &samples, &rate);
                }
                codec_decoder_end(&d);
                return g_lines;
            }""")
        lib = compile_c(cls.tmp.name, "dispatch.so",
                        ["audiohelper/CodecDecoder.c", "audiohelper/SBCDecoder.c", *DECODER_SOURCES, str(shim)],
                        shared=True, extra_includes=[vendor],
                        lenient=["audiohelper/vendor_sbc/sbc.c", "audiohelper/vendor_sbc/bits.c"])
        cls.begin = ctypes.CDLL(str(lib)).begin_codec
        cls.begin.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
        cls.begin.restype = ctypes.c_uint
        cls.log_packets = ctypes.CDLL(str(lib)).log_packets
        cls.log_packets.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint]
        cls.log_packets.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_aac_packets_are_described_in_the_log(self):
        # The evidence the next device test depends on: V94_AAC_PKT with the transport, the frames found and the
        # packet's first 64 bytes, for the first packets of a stream and then failures, within a cap.
        elements, aus = fixture_elements()
        packets = [aac_packets.element(g, config={"subframes": len(g) - 1}) for g in windows_packing(aus)]
        # One packet nothing accounts for: a version-1 StreamMuxConfig with two programs (LATM_E_LAYOUT), whose first
        # byte is no audio element either.
        w = aac_packets.W()
        w.put(0, 1); w.put(1, 1); w.put(0, 1); aac_packets.latm_get_value(w, 0xff); w.put(1, 1); w.put(0, 6); w.put(1, 4)
        bad = (w.data() + bytes(range(64)))[:40]
        packets.insert(3, bad)
        flat = b"".join(struct.pack("<I", len(x)) + x for x in packets)
        lines = (ctypes.c_char * 65536)()
        n = self.log_packets(flat, len(packets), lines, 65536)
        text = lines.value.decode()
        logged = [l for l in text.splitlines() if l.startswith("V94_AAC_PKT")]
        self.assertGreater(n, 0)
        self.assertIn("V93_CODEC aac rate=48000 channels=2", text)
        self.assertEqual(len(logged), 8 + 0)                        # the first 8 packets (the bad one is among them)
        first = logged[0]
        self.assertIn("n=1 len=%d " % len(packets[0]), first)
        self.assertIn("result=0 ", first)
        self.assertIn("transport=latm hdr=0 cfg=1", first)
        self.assertIn("aus=%d " % len(windows_packing(aus)[0]), first)
        self.assertIn("head=" + " ".join("%02x" % b for b in packets[0][:64]), first)
        line = logged[3]
        self.assertIn("result=-11 ", line)
        self.assertIn("transport=none", line)
        self.assertIn("latm_err=-3@", line)                         # LATM_E_LAYOUT, and where
        self.assertIn("head=" + " ".join("%02x" % b for b in bad), line)
        self.assertIn("status=0 ", first)                           # the decoder's own end-of-input marker reads as 0

    def test_never_decodes_aac_as_sbc(self):
        SBC, AAC, DISABLED = 0x00, 0x02, 0xffffffff
        lc_48_stereo = (ctypes.c_uint8 * 8)(0x40, 0x00, 0x84, 0x80, 0, 0, 0, 0)
        unsupported = (ctypes.c_uint8 * 8)(0x40, 0x00, 0x14, 0x80, 0, 0, 0, 0)   # 96 kHz
        self.assertEqual(self.begin(SBC, lc_48_stereo, 1), SBC)
        self.assertEqual(self.begin(AAC, lc_48_stereo, 0), DISABLED)     # AAC negotiated, switch off
        self.assertEqual(self.begin(AAC, unsupported, 1), DISABLED)      # rejected before any converter exists
        self.assertEqual(self.begin(AAC, lc_48_stereo, 1), AAC)          # decodable: AAC
        self.assertEqual(self.begin(0x7f, lc_48_stereo, 1), DISABLED)    # unknown codec: never guessed


class LATMDepacketizer(unittest.TestCase):
    """latm_parse_audiomuxelement against ffmpeg: the access units it extracts from ffmpeg's LATM stream must be
    byte-identical to the ones in ffmpeg's ADTS encode of the same audio."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = build_lib(cls.tmp.name, "shared/LATM.c")
        cls.lib.latm_parse_audiomuxelement.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                                       ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p]
        cls.lib.latm_parse_audiomuxelement.restype = ctypes.c_int
        cls.elements, cls.aus = fixture_elements()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    class LATMFrame(ctypes.Structure):
        _fields_ = [("asc", ctypes.c_uint8 * 8), ("asc_len", ctypes.c_uint), ("au_len", ctypes.c_size_t),
                    ("used_same_mux", ctypes.c_uint), ("truncated", ctypes.c_uint)]

    def parse(self, element, cap=8192):
        au = (ctypes.c_uint8 * 8192)()
        fr = self.LATMFrame()
        ok = self.lib.latm_parse_audiomuxelement(bytes(element), len(element), au, cap, ctypes.byref(fr))
        return ok, bytes(au[:fr.au_len]), fr

    def test_extracts_access_units_matching_ffmpeg_adts(self):
        self.assertEqual(len(self.elements), len(self.aus))
        self.assertGreater(len(self.elements), 10)
        asc_frames = 0
        for i, (element, want) in enumerate(zip(self.elements, self.aus)):
            ok, au, fr = self.parse(element)
            self.assertEqual(ok, 1, f"element {i}")
            self.assertEqual(au, want, f"access unit {i}")
            if fr.asc_len:
                asc_frames += 1
                self.assertEqual(bytes(fr.asc[:fr.asc_len]), bytes([0x11, 0x90]))
        self.assertGreaterEqual(asc_frames, 1)

    def test_truncated_elements_are_flagged_as_fragments(self):
        for element in (self.elements[0], self.elements[1]):   # with and without a StreamMuxConfig
            for cut in (1, 2, 3, 5, len(element) // 2, len(element) - 1):
                ok, _, fr = self.parse(element[:cut])
                self.assertEqual((ok, fr.truncated), (0, 1), f"cut at {cut} of {len(element)}")

    def test_rejects_malformed_and_out_of_scope(self):
        self.assertEqual(self.parse(b"")[0], 0)
        ok, _, fr = self.parse(bytes([0x60, 0x00, 0x00]))      # audioMuxVersionA 1: reserved, not a fragment
        self.assertEqual((ok, fr.truncated), (0, 0))
        ok, _, fr = self.parse(bytes([0x40, 0x00, 0x00]))      # audioMuxVersion 1 is supported: this is just cut short
        self.assertEqual((ok, fr.truncated), (0, 1))

    def test_au_capacity_is_respected(self):
        ok, _, fr = self.parse(self.elements[0], cap=16)       # far too small for a 288-byte access unit
        self.assertEqual((ok, fr.truncated), (0, 0))


class DecoderContract(unittest.TestCase):
    """What AACDecoder hands AudioConverter, recorded by tests/fakes: the converter must get exactly ffmpeg's access
    units, one per packet, with a packet description, from Apple's software decoder class, with the ESDS cookie."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = ctypes.CDLL(str(compile_c(cls.tmp.name, "decoder.so", [*DECODER_SOURCES, "tests/aac_decoder_driver.c"],
                                            shared=True)))
        cls.elements, cls.aus = fixture_elements()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        for name in ("fake_at_fail_specific", "fake_at_reject_cookie"):
            ctypes.c_int.in_dll(self.lib, name).value = 0
        for name in ("fake_at_created", "fake_at_disposed", "fake_at_packets", "fake_at_bad_desc",
                     "fake_at_extra_packets", "fake_at_cookie_len", "fake_at_packet_log_len"):
            ctypes.c_uint.in_dll(self.lib, name).value = 0
        self.dec = Decoder(self.lib)

    def tearDown(self):
        self.dec.destroy()

    def fake(self, name):
        return ctypes.c_uint.in_dll(self.lib, name).value

    def packets(self):
        lens = (ctypes.c_uint * 256).in_dll(self.lib, "fake_at_packet_lens")
        log = (ctypes.c_uint8 * 65536).in_dll(self.lib, "fake_at_packet_log")
        out, off = [], 0
        for i in range(min(self.fake("fake_at_packets"), 256)):
            out.append(bytes(log[off:off + lens[i]]))
            off += lens[i]
        return out

    def cookie(self):
        return bytes((ctypes.c_uint8 * 256).in_dll(self.lib, "fake_at_cookie")[:self.fake("fake_at_cookie_len")])

    def decode_all(self, elements, expect=0):
        for i, element in enumerate(elements):
            r, samples, rate, _ = self.dec.decode(element, i, i * 1024)
            self.assertEqual(r, expect, f"element {i}")
            if expect == 0:
                self.assertEqual((samples, rate), (1024, 48000))

    def test_raw_access_units_software_decoder_and_esds_cookie(self):
        self.assertTrue(self.dec.create())
        st = self.dec.status
        self.assertEqual((st.used_software, st.cookie_set, st.specific_status, st.new_status), (1, 1, 0, 0))
        self.assertEqual((self.fake("fake_at_class_type"), self.fake("fake_at_class_subtype"),
                          self.fake("fake_at_class_mfr")), (0x61646563, 0x61616320, 0x6170706c))   # adec, aac , appl
        esds = (ROOT / "tests/fixtures/aac_esds_ffmpeg.bin").read_bytes()
        self.assertEqual(self.cookie()[:5], esds[:5])
        self.assertEqual(self.cookie()[26:33], bytes([0x05, 0x80, 0x80, 0x80, 0x02, 0x11, 0x90]))
        self.decode_all(self.elements)
        self.assertEqual(self.packets(), self.aus)            # the bare access units, nothing more, nothing less
        self.assertEqual((self.fake("fake_at_bad_desc"), self.fake("fake_at_extra_packets")), (0, 0))

    def test_adts_framing_prefixes_a_valid_header(self):
        self.assertTrue(self.dec.create(framing=Decoder.ADTS, cookie=Decoder.NONE))
        self.assertEqual(self.cookie(), b"")
        self.decode_all(self.elements)
        for packet, au in zip(self.packets(), self.aus):
            self.assertEqual(packet[7:], au)
            self.assertEqual(packet[0], 0xff)
            self.assertEqual(packet[1] & 0xf6, 0xf0)
            self.assertEqual(((packet[3] & 3) << 11) | (packet[4] << 3) | (packet[5] >> 5), 7 + len(au))

    def test_asc_cookie(self):
        self.assertTrue(self.dec.create(cookie=Decoder.ASC))
        self.assertEqual(self.cookie(), bytes([0x11, 0x90]))

    def test_falls_back_when_the_software_decoder_is_unavailable(self):
        ctypes.c_int.in_dll(self.lib, "fake_at_fail_specific").value = 1
        self.assertTrue(self.dec.create())
        st = self.dec.status
        self.assertEqual((st.used_software, st.specific_status, st.new_status), (0, 0x666d743f, 0))   # 'fmt?'
        self.decode_all(self.elements[:3])

    def test_a_refused_cookie_is_not_fatal(self):
        ctypes.c_int.in_dll(self.lib, "fake_at_reject_cookie").value = 1
        self.assertTrue(self.dec.create())
        self.assertEqual((self.dec.status.cookie_set, self.dec.status.cookie_status), (0, 0x21646174))  # '!dat'
        self.decode_all(self.elements[:3])

    def test_fragments_are_reassembled(self):
        self.assertTrue(self.dec.create())
        sequence = 0
        for i, element in enumerate(self.elements):
            third = len(element) // 3
            for k, piece in enumerate((element[:third], element[third:2 * third], element[2 * third:])):
                r, samples, rate, _ = self.dec.decode(piece, sequence, i * 1024)
                sequence += 1
                self.assertEqual(r, 0 if k == 2 else 1, f"element {i} fragment {k}")
        self.assertEqual(self.packets(), self.aus)
        self.assertEqual((self.dec.stat("fragments_joined"), self.dec.stat("fragments_dropped")),
                         (len(self.elements), 0))

    def test_a_lost_fragment_costs_only_its_element(self):
        self.assertTrue(self.dec.create())
        sequence = 0
        for i, element in enumerate(self.elements):
            half = len(element) // 2
            for k, piece in enumerate((element[:half], element[half:])):
                if not (i == 5 and k == 1):                  # lose the second half of element 5
                    self.dec.decode(piece, sequence, i * 1024)
                sequence += 1
        self.assertEqual(self.packets(), self.aus[:5] + self.aus[6:])   # every other access unit, exactly

    def test_in_band_config_change_rebuilds_the_converter(self):
        self.assertTrue(self.dec.create())
        first = bytearray(self.elements[0])
        self.assertEqual(first[2:4], bytes([0x11, 0x90]))    # the StreamMuxConfig's ASC is byte-aligned here
        first[2:4] = bytes([0x12, 0x10])                      # 44.1 kHz stereo
        r, samples, rate, _ = self.dec.decode(first, 0, 0)
        self.assertEqual((r, rate, self.dec.stat("config_changes")), (0, 44100, 1))
        later = next(i for i, e in enumerate(self.elements) if i > 0 and not e[0] >> 7)   # the next StreamMuxConfig
        for i in range(1, later + 1):
            r, samples, rate, _ = self.dec.decode(self.elements[i], i, i * 1024)
            self.assertEqual(r, 0)
        self.assertEqual((rate, self.dec.stat("config_changes")), (48000, 2))

    def test_unsupported_in_band_config_is_refused(self):
        self.assertTrue(self.dec.create())
        first = bytearray(self.elements[0])
        first[2:4] = bytes([0x10, 0x10])                      # 96 kHz: not supported
        self.assertEqual(self.dec.decode(first, 0, 0)[0], -14)
        self.assertEqual(self.packets(), [])                 # nothing reached the converter

    def test_malformed_element_is_an_error(self):
        self.assertTrue(self.dec.create())
        self.assertEqual(self.dec.decode(bytes([0x40, 0x00, 0x00]), 0, 0)[0], -11)


class SelfTestRuns(unittest.TestCase):
    """BTPSAudioHelper --aac-selftest (AACSelfTest.c), built with the helper's dispatch (tests/aac_selftest_main.c).
    With tests/fakes everywhere: stages, markers, JSON, and that its checks reject bad audio. On macOS also linked
    against the real AudioToolbox, to prove it builds and creates a converter there."""
    SOURCES = ["tests/aac_selftest_main.c", "audiohelper/AACSelfTest.c"] + DECODER_SOURCES

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exe = compile_c(cls.tmp.name, "selftest-fake", cls.SOURCES)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_selftest(self, *args, exe=None, env=None):
        return subprocess.run([str(exe or self.exe), "--aac-selftest", *args], capture_output=True, text=True,
                              timeout=300, env={**os.environ, **(env or {})})

    def test_stages_markers_and_json(self):
        r = self.run_selftest("--stage=entry")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("STAGE entry", r.stderr)
        r = self.run_selftest("--stage=create")
        self.assertEqual(r.returncode, 0)
        self.assertIn("created=1", r.stderr)
        r = self.run_selftest("--stage=decode1")
        report = json.loads(r.stdout)
        self.assertEqual((r.returncode, report["pass"], report["results"][0]["elements"]), (0, None, 1))
        r = self.run_selftest()
        report = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x["strategy"] for x in report["results"]], ["raw-esds", "raw-asc", "raw-none", "adts-none"])
        self.assertEqual((report["production_strategy"], report["pass"]), ("raw-esds", True))
        for marker in ("SPEAKA_SELFTEST BEGIN", "STAGE fixture bytes=8563 elements=25 expected=25",
                       "STAGE before_first_decode strategy=raw-esds", "STAGE json", "STAGE exit code=0"):
            self.assertIn(marker, r.stderr)
        self.assertEqual(sum(line.startswith("DECODE ") for line in r.stderr.splitlines()), 100)

    def test_its_checks_reject_bad_audio(self):
        r = self.run_selftest(env={"FAKE_AT_OUTPUT": "noise"})
        report = json.loads(r.stdout)
        self.assertEqual((r.returncode, report["pass"], report["working_strategies"]), (1, False, []))
        self.assertLess(report["results"][0]["tone_snr_db"], 20.0)

    def test_single_strategy_and_usage_errors(self):
        report = json.loads(self.run_selftest("--strategy=raw-asc").stdout)
        self.assertEqual([x["strategy"] for x in report["results"]], ["raw-asc"])
        for bad in (["--bogus"], ["--stage=nope"], ["--strategy=nope"]):
            self.assertEqual(self.run_selftest(*bad).returncode, 2, bad)

    @unittest.skipUnless(IS_MAC, "the real AudioToolbox exists only on macOS")
    def test_links_and_creates_a_converter_with_the_real_audiotoolbox(self):
        exe = compile_c(self.tmp.name, "selftest-real", self.SOURCES, real_audiotoolbox=True)
        r = self.run_selftest("--stage=create", exe=exe)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("created=1", r.stderr)


class RealAudioToolbox(unittest.TestCase):
    """Apple's real AAC decoder on the fixture: the self-test must pass with the production strategy, and the PCM must
    match ffmpeg's decode of the same stream. Runs in CI's device-test job (SPEAKA_REAL_AAC=1 on macOS) and fails
    there rather than skipping if it cannot run."""
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("SPEAKA_REAL_AAC"):
            raise unittest.SkipTest("runs in CI's device-test job (SPEAKA_REAL_AAC=1, macOS)")
        if not IS_MAC:
            raise AssertionError("SPEAKA_REAL_AAC=1 needs macOS and the real AudioToolbox")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.selftest = compile_c(cls.tmp.name, "selftest-real", SelfTestRuns.SOURCES, real_audiotoolbox=True)
        cls.lib = ctypes.CDLL(str(compile_c(cls.tmp.name, "decoder.dylib",
                                            [*DECODER_SOURCES, "tests/aac_decoder_driver.c"],
                                            real_audiotoolbox=True, shared=True)))
        cls.elements, _ = fixture_elements()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_selftest_passes_with_apples_decoder(self):
        r = subprocess.run([str(self.selftest), "--aac-selftest"], capture_output=True, text=True, timeout=300)
        for line in r.stderr.splitlines():
            if "after_create" in line or "strategy_done" in line:
                print(line)                                  # which strategies Apple's decoder accepts, in the log
        report = json.loads(r.stdout)
        self.assertTrue(report["pass"], "production strategy failed; working: %s" % report["working_strategies"])

    def test_windows_packed_packets_decode_with_apples_decoder(self):
        # Several frames per packet, the configuration in every packet: what v9.8.85 rejected with -11 on the phone.
        dec = Decoder(self.lib)
        self.assertTrue(dec.create())
        _, aus = fixture_elements()
        total, timestamp = 0, 0
        for i, g in enumerate(windows_packing(aus)):
            r, samples, rate, _ = dec.decode(aac_packets.element(g, config={"subframes": len(g) - 1}), i, timestamp)
            self.assertIn(r, (0, 1), f"packet {i}: status {dec.stat('last_status')}")
            total += samples
            timestamp += 1024 * len(g)
        dec.destroy()
        self.assertGreaterEqual(total, 23 * 1024)
        print("apple decoder, Windows-packed packets: %d PCM frames from %d access units" % (total, len(aus)))

    def test_pcm_matches_ffmpegs_decode(self):
        dec = Decoder(self.lib)
        self.assertTrue(dec.create())
        left = []
        for i, element in enumerate(self.elements):
            r, samples, rate, pcm = dec.decode(element, i, i * 1024)
            self.assertIn(r, (0, 1), f"element {i}: status {dec.stat('last_status')}")
            left += pcm[0:samples * 2:2]
        dec.destroy()
        raw = (ROOT / "tests/fixtures/aac_latm_48k_stereo_ffmpeg.s16").read_bytes()
        ref = list(struct.unpack("<%dh" % (len(raw) // 2), raw))[0::2]
        onset = lambda x: next(i for i, v in enumerate(x) if abs(v) > 64)
        guess, start, length = onset(left) - onset(ref), 4096, 12000
        self.assertGreater(len(left), start + length + 2048)
        best = (-2.0, 0)
        for lag in range(guess - 64, guess + 65):           # decoders differ in delay: align before comparing
            a = left[start + lag:start + lag + length]
            b = ref[start:start + length]
            if len(a) < length:
                continue
            dot = sum(x * y for x, y in zip(a, b))
            norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b)) or 1.0
            best = max(best, (dot / norm, lag))
        a = left[start + best[1]:start + best[1] + length]
        b = ref[start:start + length]
        ratio = math.sqrt(sum(x * x for x in a) / max(1, sum(y * y for y in b)))
        print("apple vs ffmpeg: correlation %.4f at lag %d, rms ratio %.3f" % (best[0], best[1], ratio))
        self.assertGreater(best[0], 0.95)
        self.assertTrue(0.7 < ratio < 1.4, ratio)


class ConfiguredCodec(unittest.TestCase):
    """Which codec the PC chose, read from its AVDTP SET_CONFIGURATION (AVDTP 8.9 / 8.21) - protocol bytes the RX
    signaling hook already copies - instead of assuming SBC. PDUs here are built from the spec, byte by byte."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = build_lib(cls.tmp.name, "tweak/StreamControl.c", extra=("-I", str(ROOT)))
        fn = cls.lib.avdtp_setconfig_media_codec
        fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                       ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                       ctypes.POINTER(ctypes.c_size_t)]
        fn.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def setconfig(acp, intp, *capabilities, tx=5):
        body = b"".join(bytes([category, len(data)]) + bytes(data) for category, data in capabilities)
        return bytes([tx << 4 | 0x00, 0x03, acp << 2, intp << 2]) + body   # single packet, command, SET_CONFIGURATION

    def parse(self, pdu):
        seid, media, codec, n = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_size_t()
        info = (ctypes.c_uint8 * 16)()
        ok = self.lib.avdtp_setconfig_media_codec(pdu, len(pdu), ctypes.byref(seid), ctypes.byref(media),
                                                  ctypes.byref(codec), info, 16, ctypes.byref(n))
        return ok, seid.value, media.value, codec.value, bytes(info[:n.value])

    MEDIA_TRANSPORT = (0x01, b"")
    SBC = (0x07, bytes([0x00, 0x00, 0x21, 0x15, 0x02, 0x35]))        # audio, SBC, 44.1k joint stereo, bitpool 2..53
    AAC = (0x07, bytes([0x00, 0x02, 0x40, 0x00, 0x84, 0x80, 0x00, 0x00]))   # audio, AAC: MPEG-4 LC, 48k, stereo, VBR

    def test_reads_sbc(self):
        self.assertEqual(self.parse(self.setconfig(7, 1, self.MEDIA_TRANSPORT, self.SBC)),
                         (1, 7, 0, 0x00, bytes([0x21, 0x15, 0x02, 0x35])))

    def test_reads_aac_and_its_codec_specific_bytes(self):
        ok, seid, media, codec, info = self.parse(self.setconfig(7, 1, self.MEDIA_TRANSPORT, self.AAC))
        self.assertEqual((ok, seid, media, codec), (1, 7, 0, 0x02))
        self.assertEqual(info, bytes([0x40, 0x00, 0x84, 0x80, 0x00, 0x00]))   # what aac_parse_config reads

    def test_media_codec_need_not_come_first(self):
        delay_reporting = (0x08, b"")
        content_protection = (0x04, bytes([0x02, 0x00]))
        self.assertEqual(self.parse(self.setconfig(7, 1, self.MEDIA_TRANSPORT, content_protection, delay_reporting,
                                                   self.AAC))[3], 0x02)

    def test_rejects_what_is_not_a_set_configuration_with_a_codec(self):
        good = self.setconfig(7, 1, self.MEDIA_TRANSPORT, self.SBC)
        self.assertEqual(self.parse(good[:-1])[0], 0)                     # a capability runs past the PDU
        self.assertEqual(self.parse(bytes([good[0] | 0x02]) + good[1:])[0], 0)   # a response, not a command
        self.assertEqual(self.parse(bytes([good[0] | 0x04]) + good[1:])[0], 0)   # the start of a fragmented PDU
        self.assertEqual(self.parse(bytes([good[0], 0x07]) + good[2:])[0], 0)    # START, not SET_CONFIGURATION
        self.assertEqual(self.parse(self.setconfig(7, 1, self.MEDIA_TRANSPORT))[0], 0)   # no Media Codec at all
        self.assertEqual(self.parse(good[:3])[0], 0)

    def test_the_hooks_publish_the_configured_codec(self):
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        rx = hooks.split("static uint64_t hook_receive_signal(", 1)[1].split("static void install_signal_rx", 1)[0]
        # The RX hook parses the very SET_CONFIGURATION for SEID 7 it already recognises, before bluetoothd's own
        # handler runs (and so before SetConfigInd).
        self.assertLess(rx.index("(control[1]&63u)==3u && (control[2]>>2)==7u"),
                        rx.index("note_pc_configuration(control,(size_t)length)"))
        setconfig = hooks.split("static uint64_t hook_setconfigind(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("AudioRelaySetCodec(A2DP_CODEC_AAC,g_pc_config.info,AAC_CSI_BYTES)", setconfig)
        self.assertIn("AudioRelaySetCodec(codec,g_pc_config.info,g_pc_config.info_len)", setconfig)
        self.assertIn("unsigned codec=g_pc_config.valid?g_pc_config.codec:A2DP_CODEC_SBC;", setconfig)
        # The parsed codec is written and read under the stream lock.
        note = hooks.split("static void note_pc_configuration(", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(note.index("pthread_mutex_lock(&g_stream_control_lock)"), note.index("g_pc_config.valid=1"))


class AACsinkMode(unittest.TestCase):
    """The AAC-only endpoint experiment: SEID 7 advertises AAC-LC capabilities instead of SBC."""

    def test_preference_key_name(self):
        # SpeakaShared.h must define the macro (checked by the test below via the value inside it).
        shared = (ROOT / "shared/SpeakaShared.h").read_text()
        self.assertIn('"SpeakaAACSink"', shared)                       # the macro's string value
        # SPTweakClient.m must use the macro, not the bare string (enforced by test_app_speaks_the_tweaks_protocol).
        tweakc = (ROOT / "app/Speaka/Model/SPTweakClient.m").read_text()
        self.assertIn("SPEAKA_KEY_AAC_SINK", tweakc)                   # the macro name
        self.assertNotIn('"SpeakaAACSink"', tweakc)                    # not the bare string

    def test_aac_sink_capability_bytes(self):
        """In AAC mode the caps record gets codec_type=0x02, codec_data_len=6, then the AAC-LC CSI."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = build_lib(tmp, "shared/AACConfig.c")
            cap = (ctypes.c_uint8 * 6)()
            lib.aac_build_sink_capability.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            lib.aac_build_sink_capability.restype = ctypes.c_size_t
            self.assertEqual(lib.aac_build_sink_capability(cap), 6)
            self.assertEqual(cap[0] & 0xC0, 0xC0)  # MPEG-2 LC | MPEG-4 LC object types
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        aac_block = hooks.split("if (g_aac_sink_mode) {", 1)[1].split("} else {", 1)[0]
        self.assertIn("new_caps[18] = A2DP_CODEC_AAC;", aac_block)
        self.assertIn("new_caps[19] = 6u;", aac_block)
        self.assertIn("aac_build_sink_capability(kSinkAAC);", aac_block)

    def test_sbc_path_unchanged_when_aac_mode_off(self):
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        marker = "} else {\n        static const uint8_t kSinkSBC"
        sbc_block = hooks.split(marker, 1)[1].split("    }", 1)[0]
        self.assertIn("{0x31u, 0x15u, 0x02u, 0xfau}", sbc_block)
        self.assertNotIn("new_caps[18]", sbc_block)
        self.assertNotIn("new_caps[19]", sbc_block)

    def test_startup_reads_aac_sink_preference_and_logs(self):
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        self.assertIn("SPEAKA_KEY_AAC_SINK", hooks)
        self.assertIn("V93 CODEC_MODE", hooks)
        self.assertIn("V93 SEP CAPS: AAC-LC mode", hooks)
        self.assertIn("codec_mode=%s", hooks)

    def test_codec_tail_holds_either_codec(self):
        """v9.8.84 sized the codec tail for SBC (4 bytes) and copied AAC's 6 into it: 2 bytes past the allocation,
        inside bluetoothd. The tail is now a named size, and compile-time checks refuse to build if it shrinks."""
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        aac = (ROOT / "shared/AACConfig.h").read_text()
        tail = int(re.search(r"#define SINK_CODEC_TAIL_BYTES (\d+)u", hooks).group(1))
        csi = int(re.search(r"#define AAC_CSI_BYTES (\d+)u", aac).group(1))
        self.assertGreaterEqual(tail, csi)
        self.assertGreaterEqual(tail, 4)                              # kSinkSBC
        self.assertIn("calloc(1u, 3u * 16u + SINK_CODEC_TAIL_BYTES)", hooks)
        self.assertNotIn("calloc(1u, 3u * 16u + 4u)", hooks)
        self.assertIn("uint8_t kSinkAAC[AAC_CSI_BYTES];", hooks)
        self.assertIn("[(AAC_CSI_BYTES <= SINK_CODEC_TAIL_BYTES) ? 1 : -1]", hooks)
        # Every codec array copied into the tail is one of the two checked ones.
        copied = re.findall(r"memcpy\(new_caps \+ 3u \* 16u, (\w+), sizeof \1\)", hooks)
        self.assertEqual(sorted(copied), ["kSinkAAC", "kSinkSBC"])

    def test_the_aac_endpoint_switch_turns_on_aac_decoding(self):
        # Without SpeakaAAC the helper refuses an AAC stream (silence, never SBC), so the endpoint switch implies it.
        model = (ROOT / "app/Speaka/SpeakaModel.swift").read_text()
        sink = model.split("@Published var aacSink: Bool", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("SPTweakClient.setAACsinkEnabled(aacSink)", sink)
        self.assertIn("if aacSink && !aacDecode { aacDecode = true }", sink)

    def test_avdtp_registration_records_become_aac(self):
        """The records AVDTP registers for SEID 7 - what the PC reads with GET_CAPABILITIES - must say AAC-LC in AAC
        mode. v9.8.84 changed only the audio-table entry, so the PC would still have been offered SBC."""
        class SinkCaps(ctypes.Structure):
            _fields_ = [("caps_words", ctypes.c_uint64 * 6), ("codec_bytes", ctypes.c_uint8 * 8),
                        ("assigned_seid", ctypes.c_uint8)]
        with tempfile.TemporaryDirectory() as tmp:
            lib = build_lib(tmp, "tweak/SinkEndpoint.c", "shared/AACConfig.c")
            prepare, use_aac = lib.sink_prepare_caps, lib.sink_caps_use_aac
            prepare.argtypes = [ctypes.POINTER(SinkCaps), ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8)]
            use_aac.argtypes = [ctypes.POINTER(SinkCaps)]
            lib.aac_build_sink_capability.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            raw = (ctypes.c_uint8 * 48)()
            raw[0], raw[16], raw[18], raw[19], raw[32] = 1, 7, 0, 4, 8      # transport, SBC codec, delay records
            for k in range(48):
                if k not in (0, 16, 17, 18, 19, 32) and not 24 <= k < 32:   # 17: media type, must stay 0 (audio)
                    raw[k] = (k * 7) & 0xff                                    # bytes that must survive untouched
            caps = SinkCaps()
            self.assertEqual(prepare(ctypes.byref(caps), raw, (ctypes.c_uint8 * 4)(0x3f, 0x15, 0x02, 0x35)), 0)
            before = ctypes.string_at(ctypes.addressof(caps.caps_words), 48)
            self.assertEqual(use_aac(ctypes.byref(caps)), 0)
            after = ctypes.string_at(ctypes.addressof(caps.caps_words), 48)
            self.assertEqual((after[16], after[18], after[19]), (0x07, 0x02, 6))   # Media Codec record: AAC, 6 bytes
            pointer = int.from_bytes(after[24:32], "little")
            self.assertEqual(pointer, ctypes.addressof(caps.codec_bytes))
            want = (ctypes.c_uint8 * 6)()
            lib.aac_build_sink_capability(want)
            self.assertEqual(bytes(caps.codec_bytes)[:6], bytes(want))
            changed = {k for k in range(48) if before[k] != after[k]}
            self.assertLessEqual(changed, {18, 19} | set(range(24, 32)))  # nothing else in the records moves
            # Not the captured SBC records any more: refused, and nothing changes.
            self.assertEqual(use_aac(ctypes.byref(caps)), -2)
            self.assertEqual(ctypes.string_at(ctypes.addressof(caps.caps_words), 48), after)

    def test_aac_records_are_rewritten_before_anything_native_changes(self):
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        body = hooks.split("static void register_sink_after_sources(uint64_t audio_context)", 1)[1].split("\n}\n", 1)[0]
        claim = body.index("__sync_bool_compare_and_swap(&g_sink_attempted, 0, 1)")
        rewrite = body.index("sink_caps_use_aac(&g_sink_caps)")
        first_native = min(body.index("install_media_callback(avdtp_context)"), body.index("calloc(7u, 32u)"))
        self.assertLess(claim, rewrite)
        self.assertLess(rewrite, first_native)
        self.assertIn("(uint64_t)(uintptr_t)&g_sink_caps.caps_words[0], 3, 1,", body)   # these records are registered

    def test_sink_capability_advertises_a_320_kbps_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = build_lib(tmp, "shared/AACConfig.c")
            lib.aac_build_sink_capability.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            cap = (ctypes.c_uint8 * 6)()
            lib.aac_build_sink_capability(cap)
            self.assertEqual(cap[3] & 0x80, 0x80)                                       # VBR
            self.assertEqual(((cap[3] & 0x7f) << 16) | (cap[4] << 8) | cap[5], 320000)

    def test_rollback_path_is_complete(self):
        """g_aac_sink_mode is re-read from preferences at every Bluetooth restart."""
        hooks = (ROOT / "tweak/Hooks.xm").read_text()
        config_hook_body = hooks.split("static void install_config_hook(", 1)[1].split("\n}\n\n", 1)[0]
        self.assertIn("g_aac_sink_mode", config_hook_body,
                      "g_aac_sink_mode must be set inside install_config_hook (re-read on every BT restart)")


def read_aus(name):
    data = (ROOT / "tests/fixtures" / name).read_bytes()
    count, off, aus = struct.unpack_from("<I", data, 0)[0], 4, []
    for _ in range(count):
        n = struct.unpack_from("<I", data, off)[0]
        aus.append(data[off + 4:off + 4 + n])
        off += 4 + n
    return aus


def windows_packing(aus, mtu=720):
    """Group access units the way the v9.8.85 device log shows Windows packing them: as many complete frames as fit
    in one packet (about 3 per packet at low VBR rates, 1 per packet near 256 kbps)."""
    groups, current = [], []
    for au in aus:
        if current and sum(len(a) + 2 for a in current) + len(au) + 2 > mtu:
            groups.append(current)
            current = []
        current.append(au)
    if current:
        groups.append(current)
    return groups


class WindowsPacketization(unittest.TestCase):
    """The live failure: every packet from Windows was rejected with -11. The old parser accepted only one access unit
    per packet and rejected several frames per element, LOAS framing, audioMuxVersion 1, otherData and the other
    standard framings. Each shape here is built from ffmpeg's real access units with a writer ffmpeg's own LATM
    decoder accepts (tests/aac_packets.py); the depacketizer must return exactly ffmpeg's access units, in order."""
    TRANSPORT = {1: "latm", 2: "loas", 3: "adts", 4: "rfc3640", 5: "latm-oob", 6: "raw"}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = ctypes.CDLL(str(compile_c(cls.tmp.name, "packets.so", [*DECODER_SOURCES, "tests/aac_decoder_driver.c"],
                                            shared=True)))
        cls.lib.drv_packet_new.restype = ctypes.c_void_p
        cls.lib.drv_packet_new.argtypes = [ctypes.c_uint]
        cls.lib.drv_packet_free.argtypes = [ctypes.c_void_p]
        cls.lib.drv_packet_split.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                             ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint,
                                             ctypes.POINTER(ctypes.c_uint), ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
        cls.lib.drv_packet_split.restype = ctypes.c_int
        cls.elements, cls.aus = fixture_elements()
        cls.silence = read_aus("aac_silence_48k_stereo.aus")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.state = self.lib.drv_packet_new(2)

    def tearDown(self):
        self.lib.drv_packet_free(self.state)

    def split(self, packet):
        out = (ctypes.c_uint8 * 16384)()
        lens = (ctypes.c_uint * 16)()
        info = (ctypes.c_int * 7)()
        n = self.lib.drv_packet_split(self.state, bytes(packet), len(packet), out, 16384, lens, 16, info)
        aus, off = [], 0
        for i in range(max(n, 0)):
            aus.append(bytes(out[off:off + lens[i]]))
            off += lens[i]
        return n, aus, self.TRANSPORT.get(info[0], "none"), list(info)

    def expect(self, packets, want_aus, transport):
        got = []
        for i, packet in enumerate(packets):
            n, aus, t, info = self.split(packet)
            self.assertGreater(n, 0, f"packet {i} ({len(packet)} bytes) rejected: info={info}")
            self.assertEqual(t, transport, f"packet {i}")
            got += aus
        self.assertEqual(got, want_aus)

    def test_several_frames_per_packet_with_the_config_in_every_packet(self):
        groups = windows_packing(self.aus)
        self.assertTrue(any(len(g) > 1 for g in groups))
        packets = [aac_packets.element(g, config={"subframes": len(g) - 1}) for g in groups]
        self.expect(packets, self.aus, "latm")

    def test_config_once_then_reused_across_multi_frame_packets(self):
        pairs = [self.aus[i:i + 2] for i in range(0, 24, 2)]
        packets = [aac_packets.element(pairs[0], config={"subframes": 1})]
        packets += [aac_packets.element(g) for g in pairs[1:]]           # useSameStreamMux=1: needs the cached config
        self.expect(packets, [a for g in pairs for a in g], "latm")

    def test_audio_mux_version_1_and_other_data(self):
        packets = [aac_packets.element([a], config={"version": 1, "asc_fill": 5}) for a in self.aus[:5]]
        packets += [aac_packets.element([a], config={"other_bits": 300, "crc": 0x5a}, other_bits=300) for a in self.aus[5:10]]
        packets += [aac_packets.element([a], config={"version": 1, "other_bits": 24}, other_bits=24) for a in self.aus[10:15]]
        self.expect(packets, self.aus[:15], "latm")

    def test_loas_frames_from_ffmpeg_one_and_two_per_packet(self):
        loas = (ROOT / "tests/fixtures/aac_latm_48k_stereo.loas").read_bytes()
        frames, i = [], 0
        while i < len(loas):
            n = ((loas[i + 1] & 0x1f) << 8) | loas[i + 2]
            frames.append(loas[i:i + 3 + n])
            i += 3 + n
        self.expect(frames, self.aus, "loas")                             # ffmpeg's real LOAS bytes, one per packet
        self.lib.drv_packet_free(self.state)
        self.state = self.lib.drv_packet_new(2)
        self.expect([frames[i] + frames[i + 1] for i in range(0, 24, 2)], self.aus[:24], "loas")

    def test_adts_frames_from_ffmpeg(self):
        adts = (ROOT / "tests/fixtures/aac_adts_48k_stereo.aac").read_bytes()
        frames, i = [], 0
        while i + 7 <= len(adts):
            n = ((adts[i + 3] & 3) << 11) | (adts[i + 4] << 3) | (adts[i + 5] >> 5)
            frames.append(adts[i:i + n])
            i += n
        self.expect(frames, self.aus, "adts")
        self.lib.drv_packet_free(self.state)
        self.state = self.lib.drv_packet_new(2)
        self.expect([b"".join(frames[i:i + 3]) for i in range(0, 24, 3)], self.aus[:24], "adts")

    def test_rfc3640_out_of_band_latm_and_bare_access_units(self):
        self.expect([aac_packets.rfc3640(g) for g in windows_packing(self.aus)], self.aus, "rfc3640")
        for transport, packets in (("latm-oob", [aac_packets.element([a], mux_config_present=False) for a in self.aus]),
                                   ("raw", list(self.aus))):
            self.lib.drv_packet_free(self.state)
            self.state = self.lib.drv_packet_new(2)
            self.expect(packets, self.aus, transport)

    def test_one_byte_media_payload_header_must_match_its_frame_count(self):
        groups = windows_packing(self.aus)
        packets = [bytes([len(g)]) + aac_packets.element(g, config={"subframes": len(g) - 1}) for g in groups]
        self.expect(packets, self.aus, "latm")
        n, aus, t, info = self.split(packets[0])
        self.assertEqual(info[1], 1)                                      # the header was recognised
        wrong = bytes([5]) + aac_packets.element(self.aus[:2], config={"subframes": 1})
        n, aus, t, info = self.split(wrong)
        self.assertFalse(n > 0 and info[1] == 1, "a header that miscounts must not be accepted as a header")

    def test_the_16_byte_silent_packets(self):
        # ffmpeg's silent stereo frames are 6 bytes; a LOAS frame carrying one, with its StreamMuxConfig, is exactly
        # the 16 bytes seen in the device log. The other framings of the same frame must decode too.
        quiet = [a for a in self.silence if len(a) == 6][:6]
        loas = [aac_packets.loas(aac_packets.element([a], config={})) for a in quiet]
        self.assertEqual({len(p) for p in loas}, {16})
        self.expect(loas, quiet, "loas")
        for transport, packets in (("latm", [aac_packets.element([a], config={}) for a in quiet]),
                                   ("latm", [aac_packets.element(quiet[i:i + 2], config={"subframes": 1})
                                             for i in range(0, 6, 2)])):
            self.lib.drv_packet_free(self.state)
            self.state = self.lib.drv_packet_new(2)
            self.expect(packets, quiet, transport)

    def test_malformed_input_is_refused_with_a_reason(self):
        # Random bytes: a strong framing (a sync word, or length fields that must add up exactly) never accepts them.
        rng = random.Random(7)
        for i in range(300):
            self.lib.drv_packet_free(self.state)
            self.state = self.lib.drv_packet_new(2)
            junk = bytes(rng.randrange(256) for _ in range(rng.choice((2, 5, 16, 64, 300, 700))))
            n, aus, t, info = self.split(junk)
            self.assertNotIn(t, ("latm", "loas", "adts", "rfc3640"), f"junk #{i} ({len(junk)} bytes) accepted as {t}")
        # An element followed by stray bytes does not account for the packet: not LATM.
        self.lib.drv_packet_free(self.state)
        self.state = self.lib.drv_packet_new(2)
        n, aus, t, info = self.split(aac_packets.element([self.aus[0]], config={}) + b"\x00\x01\x02")
        self.assertNotEqual(t, "latm")
        # Two programs, a layout A2DP never uses: LATM refuses it and says why (LATM_E_LAYOUT).
        self.lib.drv_packet_free(self.state)
        self.state = self.lib.drv_packet_new(2)
        w = aac_packets.W()
        w.put(0, 1); w.put(0, 1); w.put(1, 1); w.put(0, 6); w.put(1, 4); w.put(0, 3)
        w.put(0x1190, 16); w.put(0, 32)
        n, aus, t, info = self.split(w.data())
        self.assertNotEqual(t, "latm")
        self.assertEqual(info[3], -3)

    def test_fragments_wait_for_the_rest(self):
        first = aac_packets.element([self.aus[0]], config={})
        self.assertGreater(self.split(first)[0], 0)                       # locks LATM
        element = aac_packets.element([self.aus[1]])
        n, aus, t, info = self.split(element[:200])
        self.assertEqual((n, info[5]), (0, 1))                            # a fragment of the locked transport
        loas = aac_packets.loas(aac_packets.element([self.aus[2]], config={}))
        self.lib.drv_packet_free(self.state)
        self.state = self.lib.drv_packet_new(2)
        self.assertEqual(self.split(loas[:100])[0], 0)                    # a LOAS sync word, cut short


class WindowsPacketsDecode(unittest.TestCase):
    """End to end through AACDecoder with tests/fakes: packets packed the way Windows packs them decode to 1024 PCM
    frames per access unit, and the converter receives exactly ffmpeg's access units. v9.8.85 returned -11 for all."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = ctypes.CDLL(str(compile_c(cls.tmp.name, "decoder.so", [*DECODER_SOURCES, "tests/aac_decoder_driver.c"],
                                            shared=True)))
        cls.elements, cls.aus = fixture_elements()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        for name in ("fake_at_packets", "fake_at_packet_log_len"):
            ctypes.c_uint.in_dll(self.lib, name).value = 0
        self.dec = Decoder(self.lib)
        self.assertTrue(self.dec.create())

    def tearDown(self):
        self.dec.destroy()

    def received(self):
        lens = (ctypes.c_uint * 256).in_dll(self.lib, "fake_at_packet_lens")
        log = (ctypes.c_uint8 * 65536).in_dll(self.lib, "fake_at_packet_log")
        out, off = [], 0
        for i in range(ctypes.c_uint.in_dll(self.lib, "fake_at_packets").value):
            out.append(bytes(log[off:off + lens[i]]))
            off += lens[i]
        return out

    def run_packets(self, groups, build):
        timestamp = 0
        for i, g in enumerate(groups):
            r, samples, rate, _ = self.dec.decode(build(g), i, timestamp)
            self.assertEqual((r, samples, rate), (0, 1024 * len(g), 48000), f"packet {i} of {len(g)} frames")
            timestamp += 1024 * len(g)
        self.assertEqual(self.received(), [a for g in groups for a in g])

    def test_multi_frame_latm_packets(self):
        self.run_packets(windows_packing(self.aus), lambda g: aac_packets.element(g, config={"subframes": len(g) - 1}))

    def test_loas_packets(self):
        self.run_packets([[a] for a in self.aus], lambda g: aac_packets.loas(aac_packets.element(g, config={})))


if __name__ == "__main__":
    unittest.main()
