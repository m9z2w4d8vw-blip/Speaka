"""AAC-LC support (experimental): the pure-C parts that run on this host.

What is tested here, with the host C compiler and independently generated fixtures:
  - the A2DP AAC Codec Specific Information: building Speaka's Sink capability, and parsing a chosen configuration;
  - the MPEG-4 AudioSpecificConfig and the sampling-frequency index;
  - the AVDTP media-codec record the tweak would advertise;
  - the ADTS framing the helper wraps each packet in;
  - the codec dispatch falling back to SBC.

What is NOT tested here, because there is no Apple toolchain on the host and no device:
  - AudioConverter actually decoding AAC to PCM (AACDecoder.c is compiled in CI, not run);
  - that Windows negotiates AAC rather than SBC (that needs the on-device procedure and a packet capture).
These are the AAC blockers; see docs/CHANGELOG.md. Every SBC test in test_audio.py still runs unchanged.
"""
import ctypes
import os
import shutil
import sys
import pathlib
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


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


class ADTSFraming(unittest.TestCase):
    """The ADTS header AACDecoder wraps each packet in is pure bit-packing; check it directly by compiling the
    builder in isolation (AudioConverter is not involved and not available on the host)."""
    def test_adts_header_fields(self):
        src = r'''
        #include <stdint.h>
        #include <stddef.h>
        #define ADTS_HEADER_BYTES 7u
        /* Copy of AACDecoder.c's build_adts, kept in sync; this is standard ISO/IEC 13818-7 bit-packing. */
        void build(uint8_t header[7], unsigned freq_index, unsigned channels, size_t frame_len) {
            size_t total = ADTS_HEADER_BYTES + frame_len; unsigned profile = 1u;
            header[0]=0xffu; header[1]=0xf1u;
            header[2]=(uint8_t)((profile<<6)|(freq_index<<2)|((channels>>2)&0x1u));
            header[3]=(uint8_t)(((channels&0x3u)<<6)|((total>>11)&0x3u));
            header[4]=(uint8_t)((total>>3)&0xffu);
            header[5]=(uint8_t)(((total&0x7u)<<5)|0x1fu);
            header[6]=0xfcu;
        }'''
        with tempfile.TemporaryDirectory() as tmp:
            c = pathlib.Path(tmp) / "adts.c"
            c.write_text(src)
            lib = pathlib.Path(tmp) / "adts.so"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                            "-o", str(lib), str(c)], check=True)
            build = ctypes.CDLL(str(lib)).build
            build.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t]
            header = (ctypes.c_uint8 * 7)()
            build(header, 3, 2, 357)                          # 48 kHz, stereo, 357-byte frame
            self.assertEqual(header[0], 0xff)
            self.assertEqual(header[1] & 0xf6, 0xf0)          # syncword + MPEG-4 + Layer 0
            self.assertEqual((header[2] >> 2) & 0x0f, 3)      # freq index
            total = 7 + 357
            carried = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
            self.assertEqual(carried, total)                  # frame length round-trips
            # channel config spans byte2 bit0 and byte3 bits7-6.
            chan = ((header[2] & 0x01) << 2) | (header[3] >> 6)
            self.assertEqual(chan, 2)


class CodecDispatchFallback(unittest.TestCase):
    """codec_decoder_begin falls back to SBC when AAC is off, mis-configured, or decoder setup fails.

    This compiles the real helper sources, so it is honest about linkage the way CI is:
      - the real vendored libsbc (audiohelper/vendor_sbc), skipped if it has not been fetched (local runs without
        tools/fetch_google_sbc.sh); CI fetches it before this step, so CI always runs this test;
      - AudioToolbox: on a platform that has the real framework (macOS), link it and use its header; elsewhere a
        small stub header with inline definitions, so there is nothing unresolved to link. The AAC decoder's
        AudioConverter calls are never reached on the SBC-fallback assertions, but the library must link cleanly.
    """
    @classmethod
    def setUpClass(cls):
        vendor = ROOT / "audiohelper/vendor_sbc"
        if not (vendor / "sbc.c").exists() or not (vendor / "bits.c").exists():
            # Locally this is a legitimate skip (no fetch). In CI libsbc is always fetched first, so skipping there
            # would hide a real link failure - fail loudly instead. (Phase 4: a test must not silently skip past a
            # missing dependency that CI is supposed to provide.)
            if os.environ.get("CI"):
                raise AssertionError("vendored libsbc missing in CI; the fetch step must run before the tests")
            raise unittest.SkipTest("vendored libsbc not present; run tools/fetch_google_sbc.sh (CI does)")
        cls.have_audiotoolbox = sys.platform == "darwin"

    def _build(self, tmp, *extra_sources, shim=None):
        """Build a .so from the real helper sources plus libsbc, with AudioToolbox real-or-stubbed. Returns its path."""
        t = pathlib.Path(tmp)
        includes = ["-I" + str(ROOT), "-I" + str(ROOT / "audiohelper"),
                    "-I" + str(ROOT / "audiohelper/vendor_sbc")]
        link = []
        if self.have_audiotoolbox:
            link = ["-framework", "AudioToolbox"]
        else:
            (t / "AudioToolbox").mkdir(exist_ok=True)
            (t / "AudioToolbox/AudioToolbox.h").write_text(
                "#pragma once\n#include <stdint.h>\n#include <stddef.h>\ntypedef int32_t OSStatus;enum{noErr=0};\n"
                "typedef uint32_t UInt32;typedef double Float64;\n"
                "typedef struct{Float64 mSampleRate;UInt32 mFormatID,mFormatFlags,mBytesPerPacket,mFramesPerPacket,"
                "mBytesPerFrame,mChannelsPerFrame,mBitsPerChannel,mReserved;}AudioStreamBasicDescription;\n"
                "typedef struct{UInt32 mNumberChannels,mDataByteSize;void*mData;}AudioBuffer;"
                "typedef struct{UInt32 mNumberBuffers;AudioBuffer mBuffers[1];}AudioBufferList;\n"
                "typedef struct{int64_t mStartOffset;UInt32 mVariableFramesInPacket,mDataByteSize;}AudioStreamPacketDescription;\n"
                "typedef struct X*AudioConverterRef;enum{kAudioFormatLinearPCM=1,kAudioFormatMPEG4AAC=2};\n"
                "enum{kAudioFormatFlagIsSignedInteger=4,kAudioFormatFlagIsPacked=8,kMPEG4Object_AAC_LC=2};\n"
                "typedef OSStatus(*AudioConverterComplexInputDataProc)(AudioConverterRef,UInt32*,AudioBufferList*,AudioStreamPacketDescription**,void*);\n"
                "static inline OSStatus AudioConverterNew(const AudioStreamBasicDescription*a,const AudioStreamBasicDescription*b,AudioConverterRef*c){(void)a;(void)b;*c=(AudioConverterRef)1;return 0;}\n"
                "static inline OSStatus AudioConverterDispose(AudioConverterRef c){(void)c;return 0;}\n"
                "static inline OSStatus AudioConverterReset(AudioConverterRef c){(void)c;return 0;}\n"
                "static inline OSStatus AudioConverterFillComplexBuffer(AudioConverterRef c,AudioConverterComplexInputDataProc p,void*x,UInt32*n,AudioBufferList*b,AudioStreamPacketDescription*d){(void)c;(void)p;(void)x;(void)b;(void)d;*n=0;return 0;}\n")
            includes = ["-I" + str(t)] + includes
        sources = [str(ROOT / "audiohelper/CodecDecoder.c"), str(ROOT / "audiohelper/AACDecoder.c"),
                   str(ROOT / "audiohelper/SBCDecoder.c"), str(ROOT / "shared/AACConfig.c"),
                   str(ROOT / "shared/LATM.c"),
                   str(ROOT / "audiohelper/vendor_sbc/sbc.c"), str(ROOT / "audiohelper/vendor_sbc/bits.c")]
        if shim:
            (t / "shim.c").write_text(shim)
            sources.append(str(t / "shim.c"))
        lib = t / "dispatch.so"
        # libsbc is upstream fixed-point C; build it without -Werror, our code with it.
        objs = []
        for i, src in enumerate(sources):
            obj = str(t / ("%d.o" % i))
            strict = [] if "/vendor_sbc/" in src else ["-Werror"]
            r = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", *strict, "-fPIC", *includes, "-c", src, "-o", obj],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            objs.append(obj)
        r = subprocess.run(["cc", "-shared", "-fPIC", *objs, *link, "-o", str(lib)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return str(lib)

    def test_defaults_to_sbc_without_aac(self):
        shim = r'''
        #include "CodecDecoder.h"
        unsigned begin_codec(unsigned codec, const unsigned char *csi, int aac_enabled) {
            AudioRing h; h.codec = codec;
            for (int i=0;i<8;i++) h.codec_config[i]=csi[i];
            CodecDecoder d; unsigned used = codec_decoder_begin(&d, &h, aac_enabled, 0);
            codec_decoder_end(&d); return used;
        }'''
        with tempfile.TemporaryDirectory() as tmp:
            begin = ctypes.CDLL(self._build(tmp, shim=shim)).begin_codec
            begin.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
            begin.restype = ctypes.c_uint
            SBC, AAC, DISABLED = 0x00, 0x02, 0xffffffff
            lc_48_stereo = (ctypes.c_uint8 * 8)(0x40, 0x00, 0x84, 0x80, 0, 0, 0, 0)
            unsupported = (ctypes.c_uint8 * 8)(0x40, 0x00, 0x14, 0x80, 0, 0, 0, 0)   # 96 kHz
            self.assertEqual(begin(SBC, lc_48_stereo, 1), SBC)      # SBC session: SBC
            # The key guarantee: AAC negotiated but undecodable must NOT fall back to SBC (that would decode AAC as
            # noise). These two paths reject before the converter is ever built, so they are DISABLED on every
            # platform, real AudioToolbox or the test's success-stub alike.
            self.assertEqual(begin(AAC, lc_48_stereo, 0), DISABLED)  # switch off
            self.assertEqual(begin(AAC, unsupported, 1), DISABLED)   # unsupported rate (rejected before converter)
            # A good config builds a decoder and selects AAC. On macOS that is the real AudioConverter; on the host
            # the test links a stub AudioConverterNew that succeeds, so the result is AAC either way. The contract
            # that matters for safety is the two DISABLED cases above: AAC is never silently treated as SBC.
            self.assertEqual(begin(AAC, lc_48_stereo, 1), AAC)



class LATMDepacketizer(unittest.TestCase):
    """The LATM AudioMuxElement parser, checked against real ffmpeg output (not our own encoder).

    Fixtures (generated once with ffmpeg, shipped so CI needs no ffmpeg):
      aac_latm_48k_stereo.loas - ffmpeg `-f latm` output: LOAS-framed AudioMuxElements;
      aac_latm_48k_stereo.aus  - ffmpeg `-f adts` output of the same audio, as raw AAC access units.
    The test strips LOAS framing, runs each AudioMuxElement through latm_parse_audiomuxelement, and asserts the
    extracted access units are byte-identical to the ADTS ones. That is an end-to-end check that depacketization
    yields valid AAC access units, which the earlier ADTS-wrapping-the-raw-payload code did not.
    """
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        lib = pathlib.Path(cls.tmp.name) / "latm.so"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-I", str(ROOT),
                        "-o", str(lib), str(ROOT / "shared/LATM.c")], check=True)
        cls.lib = ctypes.CDLL(str(lib))
        cls.lib.latm_parse_audiomuxelement.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p]
        cls.lib.latm_parse_audiomuxelement.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # LATMFrame: asc[8], asc_len, au_len (size_t), used_same_mux.
    class LATMFrame(ctypes.Structure):
        _fields_ = [("asc", ctypes.c_uint8 * 8), ("asc_len", ctypes.c_uint),
                    ("au_len", ctypes.c_size_t), ("used_same_mux", ctypes.c_uint)]

    def parse(self, element):
        au = (ctypes.c_uint8 * 8192)()
        fr = self.LATMFrame()
        buf = (ctypes.c_uint8 * len(element)).from_buffer_copy(element)
        ok = self.lib.latm_parse_audiomuxelement(buf, len(element), au, 8192, ctypes.byref(fr))
        return ok, bytes(au[:fr.au_len]), fr

    @staticmethod
    def strip_loas(stream):
        """ffmpeg LOAS: 0x2B7 (11-bit sync) + 13-bit length + AudioMuxElement."""
        out, i = [], 0
        while i + 3 <= len(stream):
            word = (stream[i] << 8) | stream[i + 1]
            if (word >> 5) != 0x2B7:
                break
            length = ((word & 0x1f) << 8) | stream[i + 2]
            out.append(stream[i + 3:i + 3 + length])
            i += 3 + length
        return out

    @staticmethod
    def adts_aus(path):
        import struct
        data = path.read_bytes()
        n = struct.unpack_from("<I", data, 0)[0]
        aus, off = [], 4
        for _ in range(n):
            length = struct.unpack_from("<I", data, off)[0]; off += 4
            aus.append(data[off:off + length]); off += length
        return aus

    def test_extracts_access_units_matching_ffmpeg_adts(self):
        loas = (ROOT / "tests/fixtures/aac_latm_48k_stereo.loas").read_bytes()
        elements = self.strip_loas(loas)
        self.assertGreater(len(elements), 10)
        expected = self.adts_aus(ROOT / "tests/fixtures/aac_latm_48k_stereo.aus")
        self.assertEqual(len(elements), len(expected))
        asc_frames = 0
        for i, (element, want) in enumerate(zip(elements, expected)):
            ok, au, fr = self.parse(element)
            self.assertEqual(ok, 1, f"element {i} failed to parse")
            self.assertEqual(au, want, f"access unit {i} does not match ffmpeg's ADTS AU")
            if fr.asc_len:
                asc_frames += 1
                self.assertEqual(bytes(fr.asc[:fr.asc_len]), bytes([0x11, 0x90]))  # 48 kHz stereo AAC-LC
        self.assertGreaterEqual(asc_frames, 1)   # at least the first element carries StreamMuxConfig

    def test_rejects_malformed_and_out_of_scope(self):
        # Empty, and a single byte that claims useSameStreamMux=1 then truncates before PayloadLengthInfo.
        self.assertEqual(self.parse(b"")[0], 0)
        self.assertEqual(self.parse(b"\x80")[0], 0)              # useSameStreamMux=1, no length info
        # useSameStreamMux=0 then an audioMuxVersion=1 (unsupported): first two bits 0 1...
        self.assertEqual(self.parse(bytes([0x40, 0x00, 0x00]))[0], 0)
        # A valid element truncated mid-AU: take a real one and cut its last byte.
        loas = (ROOT / "tests/fixtures/aac_latm_48k_stereo.loas").read_bytes()
        element = self.strip_loas(loas)[1]                       # a useSameStreamMux=1 frame
        self.assertEqual(self.parse(element[:-1])[0], 0)         # claimed AU length now overruns

    def test_au_capacity_is_respected(self):
        loas = (ROOT / "tests/fixtures/aac_latm_48k_stereo.loas").read_bytes()
        element = self.strip_loas(loas)[0]
        au = (ctypes.c_uint8 * 16)()                             # far too small for a 288-byte AU
        fr = self.LATMFrame()
        buf = (ctypes.c_uint8 * len(element)).from_buffer_copy(element)
        ok = self.lib.latm_parse_audiomuxelement(buf, len(element), au, 16, ctypes.byref(fr))
        self.assertEqual(ok, 0)                                  # refuses rather than overflowing



class OnDeviceHarnessBuilds(unittest.TestCase):
    """The on-device AAC decode harness (tools/aac_decode_test) must stay buildable and correct. CI builds it against
    the real iPhoneOS SDK, but that build is far from the test step, so a missing source, a changed decoder
    signature, or a broken fixture would only surface late. This compiles the harness logic as C on the host against
    a linkable fake AudioConverter that emits 1024 frames per access unit, runs it over the real fixture, and checks
    the accounting. It exercises the real AACDecoder.c, LATM.c, AACConfig.c and the real fixture - only AudioToolbox
    is faked, which the host cannot provide. A real-decoder run happens on the device (README.md)."""
    def test_harness_compiles_and_accounts_correctly(self):
        cc = shutil.which("cc")
        self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            # Regenerate the embedded fixture header from the shipped fixtures.
            gen = subprocess.run([sys.executable, str(ROOT / "tools/aac_decode_test/make_fixture_header.py")],
                                 capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)
            self.assertTrue((ROOT / "tools/aac_decode_test/fixture_data.h").exists())

            # A C-compilable copy of the harness: drop the two Objective-C constructs (Foundation import and
            # @autoreleasepool), and make the relative includes absolute.
            src = (ROOT / "tools/aac_decode_test/aac_decode_test.m").read_text()
            src = src.replace("#import <Foundation/Foundation.h>", "")
            src = src.replace("    @autoreleasepool {", "    {")
            src = src.replace('#include "../../shared/AACConfig.h"', '#include "%s/shared/AACConfig.h"' % ROOT)
            src = src.replace('#include "../../shared/AudioRing.h"', '#include "%s/shared/AudioRing.h"' % ROOT)
            src = src.replace('#include "../../audiohelper/AACDecoder.h"', '#include "%s/audiohelper/AACDecoder.h"' % ROOT)
            src = src.replace('#include "fixture_data.h"', '#include "%s/tools/aac_decode_test/fixture_data.h"' % ROOT)
            (tmp / "harness.c").write_text(src)

            # A linkable fake AudioConverter emitting a quiet non-silent ramp, 1024 frames per access unit.
            (tmp / "AudioToolbox").mkdir()
            (tmp / "AudioToolbox/AudioToolbox.h").write_text(
                "#pragma once\n#include <stdint.h>\ntypedef int32_t OSStatus;enum{noErr=0};\n"
                "typedef uint32_t UInt32;typedef double Float64;\n"
                "typedef struct{Float64 mSampleRate;UInt32 mFormatID,mFormatFlags,mBytesPerPacket,mFramesPerPacket,"
                "mBytesPerFrame,mChannelsPerFrame,mBitsPerChannel,mReserved;}AudioStreamBasicDescription;\n"
                "typedef struct{UInt32 mNumberChannels,mDataByteSize;void*mData;}AudioBuffer;"
                "typedef struct{UInt32 mNumberBuffers;AudioBuffer mBuffers[1];}AudioBufferList;\n"
                "typedef struct{int64_t mStartOffset;UInt32 mVariableFramesInPacket,mDataByteSize;}AudioStreamPacketDescription;\n"
                "typedef struct X*AudioConverterRef;enum{kAudioFormatLinearPCM=1,kAudioFormatMPEG4AAC=2};\n"
                "enum{kAudioFormatFlagIsSignedInteger=4,kAudioFormatFlagIsPacked=8,kMPEG4Object_AAC_LC=2};\n"
                "typedef OSStatus(*AudioConverterComplexInputDataProc)(AudioConverterRef,UInt32*,AudioBufferList*,AudioStreamPacketDescription**,void*);\n"
                "OSStatus AudioConverterNew(const AudioStreamBasicDescription*,const AudioStreamBasicDescription*,AudioConverterRef*);\n"
                "OSStatus AudioConverterDispose(AudioConverterRef);\n"
                "OSStatus AudioConverterReset(AudioConverterRef);\n"
                "OSStatus AudioConverterFillComplexBuffer(AudioConverterRef,AudioConverterComplexInputDataProc,void*,UInt32*,AudioBufferList*,AudioStreamPacketDescription*);\n")
            (tmp / "fake_at.c").write_text(
                '#include "AudioToolbox/AudioToolbox.h"\n'
                "OSStatus AudioConverterNew(const AudioStreamBasicDescription*a,const AudioStreamBasicDescription*b,AudioConverterRef*c){(void)a;(void)b;*c=(AudioConverterRef)1;return 0;}\n"
                "OSStatus AudioConverterDispose(AudioConverterRef c){(void)c;return 0;}\n"
                "OSStatus AudioConverterReset(AudioConverterRef c){(void)c;return 0;}\n"
                "OSStatus AudioConverterFillComplexBuffer(AudioConverterRef c,AudioConverterComplexInputDataProc p,void*x,UInt32*io,AudioBufferList*o,AudioStreamPacketDescription*d){"
                "(void)c;(void)p;(void)x;(void)d;UInt32 n=*io;if(n>1024)n=1024;int16_t*b=o->mBuffers[0].mData;"
                "for(UInt32 i=0;i<n*2;i++)b[i]=(int16_t)((i%256)*4);*io=n;o->mBuffers[0].mDataByteSize=n*4;return 0;}\n")

            exe = tmp / "harness"
            build = subprocess.run(
                [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I", str(tmp),
                 str(tmp / "harness.c"), str(ROOT / "audiohelper/AACDecoder.c"),
                 str(ROOT / "shared/LATM.c"), str(ROOT / "shared/AACConfig.c"), str(tmp / "fake_at.c"),
                 "-lm", "-o", str(exe)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            # pass==true only if the harness walked all 25 elements and accounted 25*1024 frames.
            self.assertIn('"elements": 25', run.stdout)
            self.assertIn('"decoded_ok": 25', run.stdout)
            self.assertIn('"errors": 0', run.stdout)
            self.assertIn('"pcm_frames": 25600', run.stdout)
            self.assertIn('"decoded_rate": 48000', run.stdout)
            self.assertIn('"pass": true', run.stdout)
            self.assertEqual(run.returncode, 0, run.stdout)


if __name__ == "__main__":
    unittest.main()
