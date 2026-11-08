"""The tweak inside bluetoothd: the build check, the hooks, and the speaker endpoint's pure helpers.

Sections, in order:
  - what the hook file must and mustn't contain
  - each hook's guards stay in their own function
  - reading capability records, and their names in the log
  - the speaker endpoint's capability list
  - bluetoothd's endpoint table, before it grows
  - the allocator's geometry
  - the endpoint-config getter hook
  - the seven-slot config copy, and when it's allowed
  - the Audio Sink SDP record
  - the SDP hook: pass-through, and publishing the Sink record
  - registering the Sink after iOS's six Sources
  - the media hook
"""
import ctypes
import hashlib
import os
import pathlib
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from ctypes import CDLL, Structure, POINTER, c_ubyte, c_uint, c_size_t, c_int, byref
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = SOURCE = (ROOT / "tweak/Hooks.xm").read_text(encoding="utf-8")


# ---- what the hook file must and mustn't contain ----
# Static regression assertions for the production daemon hook source.

class SourceSafety(unittest.TestCase):
    def test_v7_tag(self):
        self.assertRegex(SOURCE, r'#define TWEAK_BUILD_TAG "v[0-9]+\.[0-9]+[^"]*"')
    def test_original_registration_called_once(self):
        hook=SOURCE.split('static uint64_t hook_register(',1)[1].split('/* ------------------------------------------------------------------------------------------------ v7 SDP observer',1)[0]
        # One pass-through Source registration and one guarded, separate trial.
        self.assertEqual(hook.count('g_orig[HOOK_REGISTER]('),1)
        self.assertIn('sizeof seid_before',hook)
        self.assertIn('snapshot_table_capacity();',hook)
        self.assertIn('if (sources == 6u)',hook)
        self.assertNotIn('g_v77_caps.assigned_seid',SOURCE)
    def test_codec_uses_observed_byte_two(self):
        self.assertIn('uint8_t codec_type;', (ROOT/'tweak/Tweak.h').read_text())
        self.assertIn('out->codec_type = buffer[off + 2]', (ROOT/'tweak/SinkEndpoint.c').read_text())
    def test_no_unverified_sdp_api_calls(self):
        self.assertNotIn('OI_SDPDB_AddServiceRecord(',SOURCE)
        self.assertIn('diag.sdp_sink_0x110b',SOURCE)
    def test_heartbeat_passive_and_scheduled(self):
        log=(ROOT/'tweak/Log.c').read_text()
        self.assertIn('HEARTBEAT bluetoothd',log)
        self.assertIn('60LL * NSEC_PER_SEC',log)
        self.assertIn('static void heartbeat_cb',log)
    def test_makefile_includes_diagnostics(self):
        self.assertIn('tweak/SinkEndpoint.c',(ROOT/'Makefile').read_text())


# ---- each hook's guards stay in their own function ----
# Regression for v8.1 CI failure: a getter-hook guard was copied into v7.8.
#
# The v7.8 capacity inspector verifies only register-prologue code that is not
# hooked.  The v7.9 geometry inspector *does* inspect the getter entry, so it
# needs the post-hook signature exception, declared in the proper C++ scope.

def function_block(start, stop):
    a = SOURCE.index(start)
    b = SOURCE.index(stop, a)
    return SOURCE[a:b]


class TweakScope(unittest.TestCase):
    def test_capacity_audit_has_no_config_getter_guard(self):
        body = function_block('static int table_signature_matches(void)',
                              'static void snapshot_table_capacity(void)')
        self.assertNotIn('g_config_hook_for_audit', body)
        self.assertNotIn('g_initial_signature_verified', body)
        self.assertNotIn('0x1f5f20u', body)
        self.assertIn('0x0cee60u', body)

    def test_allocator_audit_retains_original_getter_guard(self):
        body = function_block('static int allocator_signatures_match(void)',
                              'static void snapshot_allocation(void)')
        self.assertIn('g_config_hook_for_audit()', body)
        self.assertIn('g_initial_signature_verified', body)
        self.assertIn('s[i].offset == 0x1f5f20u', body)
        self.assertLess(SOURCE.index('static int g_config_hook_for_audit(void);'),
                        SOURCE.index('static int allocator_signatures_match(void)'))
        self.assertLess(SOURCE.index('static int g_initial_signature_verified = 0;'),
                        SOURCE.index('static int allocator_signatures_match(void)'))

    def test_capacity_function_compiles_as_cpp(self):
        compiler = shutil.which('clang++') or shutil.which('c++')
        self.assertIsNotNone(compiler, 'Host C++ compiler required to detect scope errors')
        func = function_block('static int table_signature_matches(void)',
                              'static void snapshot_table_capacity(void)')
        skeleton = '''#include <cstdint>\n#include <cstddef>\n'''
        skeleton += '''static int g_exact_build=1;\nstatic uintptr_t g_image_base=0;\nstatic int safe_peek(uint64_t, void*, size_t) { return 1; }\n'''
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp, 'capacity_probe.cpp')
            path.write_text(skeleton + func, encoding='utf-8')
            result = subprocess.run([compiler, '-std=c++17', '-fsyntax-only',
                                     '-Werror', str(path)], capture_output=True,
                                    text=True)
            self.assertEqual(result.returncode, 0,
                             'The v7.8 capacity function failed to compile: ' + result.stderr)


# ---- reading capability records, and their names in the log ----
# Portable C capability reader tests, compiled with the host C compiler.

class Caps(Structure):
    _fields_ = [
        ("total_records", c_uint), ("transport_records", c_uint),
        ("codec_records", c_uint), ("delay_records", c_uint),
        ("unknown_records", c_uint), ("codec_present", c_uint),
        ("media_type", c_ubyte), ("codec_type", c_ubyte),
        ("codec_data_len", c_ubyte), ("fingerprint", c_uint)
    ]


class CapabilityNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        dylib = Path(cls.tmp.name) / ('libdiag.dylib' if os.uname().sysname == 'Darwin' else 'libdiag.so')
        subprocess.run([shutil.which('clang') or shutil.which('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', '-I', str(ROOT), str(ROOT / 'tweak/SinkEndpoint.c'), str(ROOT / 'shared/AACConfig.c'),
                        '-o', str(dylib)], check=True)
        cls.lib = CDLL(str(dylib))
        cls.lib.avdtp_decode_caps.argtypes = [POINTER(c_ubyte), c_size_t, POINTER(Caps)]
        cls.lib.avdtp_decode_caps.restype = c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, code, size):
        payload = bytearray(48)
        payload[0] = 1
        payload[16:21] = bytes([7, 0, code, size, 0])
        payload[32] = 8
        buf = (c_ubyte * 48).from_buffer_copy(payload)
        result = Caps()
        self.assertEqual(self.lib.avdtp_decode_caps(buf, 48, byref(result)), 1)
        self.assertEqual(result.transport_records, 1)
        self.assertEqual(result.codec_records, 1)
        self.assertEqual(result.delay_records, 1)
        self.assertEqual(result.total_records, 3)
        self.assertEqual(result.codec_data_len, size)
        self.assertNotEqual(result.fingerprint, 0)
        return result

    def test_sbc(self): self.assertEqual(self.read(0x00, 4).codec_type, 0x00)
    def test_aac(self): self.assertEqual(self.read(0x02, 6).codec_type, 0x02)
    def test_vendor(self): self.assertEqual(self.read(0xff, 14).codec_type, 0xff)

    def test_malformed_length(self):
        b = (c_ubyte * 2)(1, 7)
        result = Caps()
        self.assertEqual(self.lib.avdtp_decode_caps(b, 2, byref(result)), 0)
        self.assertEqual(result.total_records, 0)

    def test_null_argument(self):
        out = Caps()
        self.assertEqual(self.lib.avdtp_decode_caps(None, 48, byref(out)), 0)

    def test_same_caps_have_same_fingerprint(self):
        self.assertEqual(self.read(0x02, 6).fingerprint, self.read(0x02, 6).fingerprint)


# ---- the speaker endpoint's capability list ----
# V7.7 opt-in Sink experiment: native C capability ownership and source safety.

class SinkCaps(ctypes.Structure):
    _fields_=[('caps_words',ctypes.c_uint64*6),('codec_bytes',ctypes.c_uint8*8),('assigned_seid',ctypes.c_uint8)]


class SinkCapabilityList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        lib=Path(cls.tmp.name)/'caps.so'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        '-o',str(lib),str(ROOT/'tweak/SinkEndpoint.c'),str(ROOT/'shared/AACConfig.c')],check=True)
        cls.func=ctypes.CDLL(str(lib)).sink_prepare_caps
        cls.func.argtypes=[ctypes.POINTER(SinkCaps),ctypes.POINTER(ctypes.c_uint8),ctypes.POINTER(ctypes.c_uint8)]
        cls.func.restype=ctypes.c_int
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def test_native_sbc_capability_is_copied_to_owned_memory(self):
        src=(ctypes.c_uint8*48)()
        src[0]=1;src[16]=7;src[18]=0;src[19]=4;src[32]=8
        codec=(ctypes.c_uint8*4)(0x3f,0x15,0x02,0x35)
        result=SinkCaps()
        self.assertEqual(self.func(ctypes.byref(result),src,codec),0)
        ptr=int.from_bytes(ctypes.string_at(ctypes.addressof(result.caps_words)+24,8),'little')
        self.assertEqual(ptr,ctypes.addressof(result.codec_bytes))
        self.assertNotEqual(ptr,ctypes.addressof(codec))
        self.assertEqual(bytes(result.codec_bytes)[:4],bytes(codec))
        src[18]=2;codec[0]=0
        self.assertEqual(bytes(result.codec_bytes)[:4],bytes((0x3f,0x15,0x02,0x35)))
        self.assertEqual(bytes(ctypes.string_at(ctypes.addressof(result.caps_words),48))[18],0)
        self.assertEqual(result.assigned_seid,0)
    def test_reject_non_sbc_or_malformed_caps(self):
        src=(ctypes.c_uint8*48)();src[0]=1;src[16]=7;src[18]=2;src[19]=4;src[32]=8
        codec=(ctypes.c_uint8*4)(0x3f,0x15,0x02,0x35)
        result=SinkCaps()
        self.assertNotEqual(self.func(ctypes.byref(result),src,codec),0)
    def test_v77_trial_retired_from_settings(self):
        pl=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertFalse(any(x.get('key')=='SpeakaV77SinkTrial' for x in pl['items']))
    def test_v77_mutation_disabled_after_error_0x68(self):
        src=(ROOT/'tweak/Hooks.xm').read_text()
        self.assertIn('snapshot_table_capacity();',src)
    def test_v78_capacity_logger_is_in_build(self):
        self.assertIn('tweak/SinkEndpoint.c',(ROOT/'Makefile').read_text())
        self.assertIn('diag.v78_table',(ROOT/'tweak/Hooks.xm').read_text())


# ---- bluetoothd's endpoint table, before it grows ----
# Read-only native slot decoder, source gating and binary-provenance checks.

class CStats(ctypes.Structure):
    _fields_ = [('total',ctypes.c_uint), ('occupied',ctypes.c_uint),
                ('vacant',ctypes.c_uint), ('duplicate_seids',ctypes.c_uint),
                ('seids',ctypes.c_uint8*32)]


class TableCapacity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = tempfile.TemporaryDirectory()
        target = Path(cls.t.name)/'capacity.so'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        str(ROOT/'tweak/SinkEndpoint.c'),str(ROOT/'shared/AACConfig.c'),'-o',str(target)],check=True)
        cls.analyze = ctypes.CDLL(str(target)).sink_analyze_table
        cls.analyze.argtypes = [ctypes.POINTER(ctypes.c_uint8),ctypes.c_size_t,ctypes.c_uint,ctypes.POINTER(CStats)]
        cls.analyze.restype=ctypes.c_int
    @classmethod
    def tearDownClass(cls): cls.t.cleanup()
    def make(self, values):
        raw=(ctypes.c_uint8*(40*len(values)))()
        for i,v in enumerate(values):raw[i*40+25]=v
        return raw
    def test_full_six_slots(self):
        mem=self.make([1,2,3,4,5,6]);out=CStats()
        self.assertEqual(self.analyze(mem,len(mem),6,ctypes.byref(out)),0)
        self.assertEqual((out.total,out.occupied,out.vacant,out.duplicate_seids),(6,6,0,0))
        self.assertEqual(list(out.seids)[:6],[1,2,3,4,5,6])
    def test_mixed_slots_and_duplicate(self):
        mem=self.make([1,0,1,7]);o=CStats()
        self.assertEqual(self.analyze(mem,len(mem),4,ctypes.byref(o)),0)
        self.assertEqual((o.occupied,o.vacant,o.duplicate_seids),(3,1,1))
    def test_reject_bad_buffer(self):
        mem=self.make([1,2]);o=CStats()
        self.assertNotEqual(self.analyze(mem,len(mem)-1,2,ctypes.byref(o)),0)
        self.assertNotEqual(self.analyze(mem,len(mem),33,ctypes.byref(o)),0)
        self.assertNotEqual(self.analyze(mem,len(mem),0,ctypes.byref(o)),0)
    def test_source_safety(self):
        src=(ROOT/'tweak/Hooks.xm').read_text()
        self.assertRegex(src, r'#define TWEAK_BUILD_TAG "v[0-9]+\.[0-9]+[^"]*"')
        self.assertIn('snapshot_table_capacity();',src)
        self.assertIn('if (sources == 6u)',src)
        self.assertIn('g_exact_build = uuid_ok;',src)
        self.assertIn('V78_TABLE RESULT',src)
        self.assertIn('tweak/SinkEndpoint.c',(ROOT/'Makefile').read_text())
        self.assertIn('#define AVDTP_TSEP_SNK 1u',src)
        self.assertIn('decisive_requested && g_seven_armed && !SpeakaExperimentsEnabled()',src)
        self.assertIn('g_native_source_success != 6',src)
    def test_settings_no_trial_toggle(self):
        d=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertNotIn('SpeakaV77SinkTrial',[x.get('key') for x in d['items']])
    def test_pinned_disassembly_layout(self):
        docs=(ROOT/'docs/BLUETOOTHD.md').read_text()
        for x in ['0xCEE14','0xCEE54','0xCEEE8','0x9768b0']:
            self.assertIn(x,docs)


# ---- the allocator's geometry ----
# v7.9 allocation geometry: native C checks, binary provenance and safe gates.

class Geometry(ctypes.Structure):
    _fields_ = [
        ('configured', ctypes.c_uint8 * 3),
        ('observed', ctypes.c_uint8 * 3),
        ('bytes', ctypes.c_uint * 3),
        ('config_matches_context', ctypes.c_uint),
        ('missing_table_mask', ctypes.c_uint),
    ]


class AllocatorGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        lib = Path(cls.temp.name) / 'libv79.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(ROOT / 'tweak/SinkEndpoint.c'), str(ROOT / 'shared/AACConfig.c'), '-o', str(lib)], check=True)
        cls.func = ctypes.CDLL(str(lib)).sink_analyze_allocation
        cls.func.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8),
                             ctypes.c_uint, ctypes.POINTER(Geometry)]
        cls.func.restype = ctypes.c_int
    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
    def check(self, expected, live, mask):
        x=(ctypes.c_uint8*3)(*expected)
        y=(ctypes.c_uint8*3)(*live)
        out=Geometry()
        return self.func(x,y,mask,ctypes.byref(out)),out
    def test_expected_six_slots(self):
        rc,g = self.check([6,8,12],[6,8,12],7)
        self.assertEqual(rc,0)
        self.assertEqual(list(g.bytes),[240,1152,768])
        self.assertEqual(g.config_matches_context,1)
        self.assertEqual(g.missing_table_mask,0)
    def test_mismatch_detected(self):
        rc,g=self.check([7,8,12],[6,8,12],7)
        self.assertEqual(rc,0)
        self.assertEqual(g.config_matches_context,0)
    def test_missing_pointer_is_reported(self):
        rc,g=self.check([6,8,12],[6,8,12],3)
        self.assertEqual(rc,0)
        self.assertEqual(g.missing_table_mask,4)
    def test_zero_secondary_arrays_are_permitted(self):
        rc,g=self.check([6,0,0],[6,0,0],1)
        self.assertEqual(rc,0)
        self.assertEqual(list(g.bytes),[240,0,0])
        self.assertEqual(g.missing_table_mask,0)
    def test_invalid_config_refused(self):
        self.assertNotEqual(self.check([65,1,1],[6,1,1],7)[0],0)
        self.assertNotEqual(self.check([6,1,1],[0,1,1],7)[0],0)
        self.assertNotEqual(self.check([6,1,1],[6,1,1],8)[0],0)
    def test_source_uses_only_memory_reads(self):
        source=(ROOT/'tweak/Hooks.xm').read_text()
        block=source.split('static void snapshot_allocation(void)',1)[1].split('/* v8.0/v8.1: Optional config getter observer',1)[0]
        self.assertIn('g_exact_build',source)
        self.assertIn('allocator_signatures_match()',block)
        self.assertIn('safe_peek',block)
        self.assertIn('sink_analyze_allocation(',block)
        self.assertNotIn('MSHookFunction',block)
        self.assertNotIn('malloc(',block)
        self.assertNotIn('memcpy(',block)
        self.assertNotIn('g_orig[',block)
        self.assertIn('if (sources == 6u)',source)
        self.assertIn('snapshot_allocation();',source)
        self.assertIn('tweak/SinkEndpoint.c', (ROOT/'Makefile').read_text())
        self.assertIn('diag.v79_allocator',source)
    def test_provenance_instruction_table(self):
        # We do not ship the user's proprietary 10MB bluetoothd in GitHub.
        # Verifies source pins sites analyzed from the exact UUID only.
        source=(ROOT/'tweak/Hooks.xm').read_text()
        for offset in ['0x0ce3f8u', '0x0ce3fcu', '0x0ce41cu', '0x0ce424u',
                       '0x0ce470u', '0x0ce4d4u', '0x0ce4f4u', '0x1f5f48u',
                       '0x984a60u', '0x9768b0u']:
            self.assertIn(offset, source)
        self.assertIn('v8.8-continuous-sbc-ring', source)
    def test_documented_limitations(self):
        docs=(ROOT/'docs/BLUETOOTHD.md').read_text()
        for needle in ['0xCE428', '0xCE4F4', '0x984a60', 'NOT', 'no allocation']:
            self.assertIn(needle,docs)


# ---- the endpoint-config getter hook ----
# v8.0 static safety gates and binary-signature provenance.

class ConfigGetter(unittest.TestCase):
    def test_exact_binary_function_entry(self):
        self.assertIn('0x001f5f20u', SRC)
        self.assertIn('0xd503237fu, 0xa9be4ff4u, 0xa9017bfdu, 0x910043fdu',SRC)
        self.assertIn('CONFIG_GETTER_SIG',SRC)
        self.assertIn('memcmp(observed, CONFIG_GETTER_SIG, sizeof observed)', SRC)
        self.assertIn('!uuid_ok',SRC)
    def test_opt_in_default_off(self):
        # v9.8: no switch for it in Settings; without the preference the observer stays off.
        prefs=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertEqual([e for e in prefs['items'] if e.get('key')=='SpeakaV80ConfigLookupObserver'],[])
        self.assertIn('SPEAKA_KEY_CONFIG_OBSERVER', (ROOT/'shared/SpeakaShared.h').read_text())
        self.assertIn('if (!config_observer_requested() && !g_seven_armed)',SRC)
    def test_observer_does_not_change_input_output_or_register_sink(self):
        block=SRC.split('static uint64_t hook_get_config(',1)[1].split('static int config_observer_requested(void)',1)[0]
        self.assertIn('g_config_get_orig(a0,a1,a2,a3,a4,a5,a6,a7)',block)
        self.assertIn('return value;',block)
        self.assertNotRegex(block,r'\b(memcpy|memmove|memset|malloc|free|realloc|vm_write|vm_protect|AVDTP_RegisterMultipoint)\s*\(')
        self.assertNotIn('g_orig[HOOK_REGISTER](',block)
        self.assertNotIn('MSHookFunction',block)
    def test_installation_order_and_baseline_unchanged(self):
        ctor=SRC.split('%ctor {',1)[1]
        self.assertLess(ctor.index('install_hooks((uintptr_t)mh, uuid_ok)'),
                        ctor.index('install_config_hook((uintptr_t)mh, uuid_ok)'))
        self.assertEqual(SRC.count('hook_fn[HOOK_REGISTER]'),1)
        self.assertIn('decisive_requested && g_seven_armed && !SpeakaExperimentsEnabled()',SRC)
        self.assertIn('V83 SEP RESULT',SRC)
        self.assertIn('snapshot_allocation();',SRC)
        self.assertIn('if (g_config_hook_enabled && g_config_index3_calls == 0u)',SRC)
    def test_docs_disclose_limitations(self):
        d=(ROOT/'docs/BLUETOOTHD.md').read_text()
        self.assertIn('No memory changes',d)
        self.assertIn('NO_INDEX3_CALL_OBSERVED',d)
        self.assertIn('Legacy Sink',d)


# ---- the seven-slot config copy, and when it's allowed ----
# Native validation and guardrails for v8.1 one-shot allocation-only trial.

class SevenSlots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        lib = Path(cls.tmp.name)/'v81config.so'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-shared','-fPIC',
                        str(ROOT/'tweak/SinkEndpoint.c'),str(ROOT/'shared/AACConfig.c'),'-o',str(lib)],check=True)
        cls.fn = ctypes.CDLL(str(lib)).sink_prepare_config_shadow
        cls.fn.argtypes=[ctypes.POINTER(ctypes.c_uint8),ctypes.c_size_t,
                         ctypes.POINTER(ctypes.c_uint8),ctypes.c_int]
        cls.fn.restype=ctypes.c_int
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def call(self,header=(6,3,3),arm=1,length=32):
        buf=(ctypes.c_uint8*32)(*(list(header)+list(range(3,32))))
        out=(ctypes.c_uint8*32)()
        n=self.fn(buf,length,out,arm)
        return n, bytes(buf), bytes(out)
    def test_seven_only_changes_first_byte(self):
        rc,original,altered=self.call()
        self.assertEqual(rc,0)
        self.assertEqual(altered[:3],b'\x07\x03\x03')
        self.assertEqual(altered[3:],original[3:])
        self.assertEqual(original[:3],b'\x06\x03\x03')
    def test_six_clone_is_identical(self):
        rc,original,altered=self.call(arm=0)
        self.assertEqual(rc,0)
        self.assertEqual(altered,original)
    def test_reject_unexpected_config(self):
        for hdr in ((7,3,3),(6,4,3),(6,3,4),(0,3,3)):
            self.assertNotEqual(self.call(header=hdr)[0],0)
    def test_reject_invalid_size_or_mode(self):
        for mode,length in ((3,32),(-1,32),(1,3),(1,31)):
            self.assertNotEqual(self.call(arm=mode,length=length)[0],0)
    def test_eight_for_the_dual_layout_only_changes_first_byte(self):
        # v9.9.11: AAC-LC + SBC Sinks: two extra endpoint slots. Byte 0 is the count the initializer allocates
        # (x 40-byte slots) and stores at +0x60; the other two allocations' counts stay as they are.
        rc,original,altered=self.call(arm=2)
        self.assertEqual(rc,0)
        self.assertEqual(altered[:3],b'\x08\x03\x03')
        self.assertEqual(altered[3:],original[3:])
    def test_default_off_and_registration_requires_separate_one_shot_arm(self):
        # v9.8: only Start arms it (tweak/Session.c), so Settings has no switch for it.
        plist=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertEqual([x for x in plist['items'] if x.get('key')=='SpeakaV81SevenOnce'],[])
        self.assertIn('CFPreferencesSetAppValue(CFSTR(SPEAKA_KEY_SEVEN_SLOTS_ONCE), arm, SESSION_DOMAIN);',
                      (ROOT/'tweak/Session.c').read_text())
        self.assertIn('decisive_requested && g_seven_armed && !SpeakaExperimentsEnabled()',SRC)
        self.assertIn('!g_signal_rx_hook_ok || !g_sdp_hook_ok',SRC)
        self.assertIn('g_native_source_success != 6',SRC)
        self.assertIn('g_orig[HOOK_REGISTER]',SRC)
    def test_one_shot_cleared_before_hook(self):
        disarm=SRC.index('consume_seven_slots_arm();')
        install=SRC.index('MSHookFunction((void *)target, (void *)hook_get_config')
        self.assertLess(disarm,install)
        block=SRC.split('static int consume_seven_slots_arm(void)',1)[1].split('static uint64_t try_seven_slots(',1)[0]
        self.assertIn('CFPreferencesSetAppValue(',block)
        self.assertIn('kCFBooleanFalse',block)
        self.assertIn('CFPreferencesAppSynchronize(',block)
        self.assertIn('!synced || !disarmed',block)
    def test_signature_precheck_and_avoid_hook_collision(self):
        self.assertIn('g_initial_signature_verified = allocator_signatures_match()',SRC)
        self.assertIn('if (s[i].offset == 0x1f5f20u && g_config_hook_for_audit())',SRC)
        self.assertIn('if (!g_initial_signature_verified) return 0;',SRC)
        self.assertIn('g_initial_signature_verified && g_exact_build',SRC)
        self.assertIn('table_original == original',SRC)
    def test_separate_static_shadow_not_live_mutation(self):
        self.assertIn('static uint8_t g_config_shadow[SINK_CONFIG_COPY_BYTES]',SRC)
        self.assertIn('sink_prepare_config_shadow(source, sizeof source, g_config_shadow, g_dual_sink ? 2 : 1)',SRC)
        self.assertIn('__sync_bool_compare_and_swap(&g_seven_applied, 0u, 1u)',SRC)
        self.assertNotIn('vm_write(',SRC)
        self.assertNotIn('vm_protect(',SRC)
        self.assertIn('tweak/SinkEndpoint.c', (ROOT/'Makefile').read_text())
        self.assertIn('V81_LIVE RESULT',SRC)
        self.assertIn('g_config_shadow_returned',SRC)
    def test_documents_disclose_no_sink_and_rollback(self):
        docs=(ROOT/'docs/BLUETOOTHD.md').read_text()
        for term in ('OFF','Legacy Sink','one-shot','v8.0','no Sink', 'slots=7'):
            self.assertIn(term,docs)


# ---- the Audio Sink SDP record ----
# Build regression only. NOT evidence of live Bluetooth or SDP discoverability.

EXPECTED={
    0xCECF0:(0xd503237f,0xd102c3ff,0xa9056ffc,0xa90667fa),
    0x1F5F20:(0xd503237f,0xa9be4ff4,0xa9017bfd,0x910043fd),
    0x1E8E78:(0xd503237f,0xa9bd57f6,0xa9014ff4,0xa9027bfD),
    0xD3B08:(0xd503237f,0xd10203ff,0xa90557f6,0xa9064ff4),
}


class Elem(ctypes.Structure):
    _fields_=[('type',ctypes.c_uint8),('reserved',ctypes.c_uint8),('length',ctypes.c_uint16),
              ('padding',ctypes.c_uint32),('value',ctypes.c_uint64)]


class Attr(ctypes.Structure):
    _fields_=[('id',ctypes.c_uint16),('padding',ctypes.c_uint8*6),('element',Elem)]


class Descriptor(ctypes.Structure):
    _fields_=[('a',ctypes.POINTER(Attr)),('count_a',ctypes.c_uint16),('reserved_a',ctypes.c_uint8*6),
              ('b',ctypes.POINTER(Attr)),('count_b',ctypes.c_uint16),('reserved_b',ctypes.c_uint8*6)]


class SinkSDPRecord(unittest.TestCase):
    def test_abi_layout_and_nested_values(self):
        self.assertEqual((ctypes.sizeof(Elem),ctypes.sizeof(Attr),ctypes.sizeof(Descriptor)),(16,24,32))
        cc=shutil.which('clang') or shutil.which('cc')
        if not cc: self.skipTest('no native compiler')
        with tempfile.TemporaryDirectory() as td:
            so=pathlib.Path(td)/'sdp.so'
            subprocess.run([cc,'-shared','-fPIC','-o',str(so),str(ROOT/'tweak/SinkEndpoint.c'),str(ROOT/'shared/AACConfig.c')],check=True,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            lib=ctypes.CDLL(str(so));lib.SinkSDPDescriptor.restype=ctypes.POINTER(Descriptor)
            desc=lib.SinkSDPDescriptor().contents
            self.assertEqual((desc.count_a,desc.count_b),(4,1))
            attrs=[desc.a[i] for i in range(4)]
            self.assertEqual([a.id for a in attrs],[1,4,9,0x311])
            def nested(elem):
                self.assertEqual(elem.type,6)
                return ctypes.cast(elem.value,ctypes.POINTER(Elem))
            self.assertEqual(nested(attrs[0].element)[0].value,0x110b)
            protos=nested(attrs[1].element)
            self.assertEqual(tuple(nested(protos[0])[i].value for i in range(2)),(0x100,0x19))
            self.assertEqual(tuple(nested(protos[1])[i].value for i in range(2)),(0x19,0x103))
            profile=nested(attrs[2].element)
            self.assertEqual(tuple(nested(profile[0])[i].value for i in range(2)),(0x110d,0x103))
            self.assertEqual((attrs[3].element.type,attrs[3].element.length,attrs[3].element.value),(1,2,1))
            name=desc.b[0]
            self.assertEqual(name.id,0x100)
            self.assertEqual(ctypes.string_at(name.element.value,name.element.length),b'Audio Sink')
    def test_binary_signatures_if_uploaded_present(self):
        b=pathlib.Path(os.environ.get('SPEAKA_BLUETOOTHD', str(ROOT/'docs/reference-binaries/ios17-iphone13/bluetoothd')))
        if not b.exists(): self.skipTest('device binary is not committed to GitHub; verify locally')
        data=b.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
            'e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6')
        for off,words in EXPECTED.items(): self.assertEqual(struct.unpack_from('<4I',data,off),words)
    def test_trial_requires_both_arms_and_verified_hooks(self):
        s=(ROOT/'tweak/Hooks.xm').read_text()
        self.assertIn('decisive_requested && g_seven_armed && !SpeakaExperimentsEnabled()',s)
        self.assertIn('!g_signal_rx_hook_ok || !g_sdp_hook_ok',s)
        self.assertIn('g_native_source_success != 6',s)
        self.assertIn('SinkSDPDescriptor()',s)
        self.assertIn('SIGNAL_RX_DISPATCH 0x000d3b08ul',s)
        self.assertNotIn('MEDIA_BYTES_RECEIVED=PASS',s)


# ---- the SDP hook: pass-through, and publishing the Sink record ----
# Static regression guarantees for the optional v7 private SDP observer.

MARKER='/* --------------------------------------------------------------------------------------- v7 SDP observer'


OBSERVER=SRC.split(MARKER,1)[1].split('/* ------------------------------------------------------------------------------------------------ installing */',1)[0]


class OptionalSDPProbe(unittest.TestCase):
    def test_optional_and_off_by_default(self):
        # v9.8: no switch for it in Settings; without the preference the probe stays off.
        d=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertEqual([x for x in d['items'] if x.get('key')=='SpeakaSDPProbe'],[])
        self.assertIn('if (!sdp_probe_requested() && !g_sink_armed)',OBSERVER)
        self.assertIn('SpeakaSetStatus("diag.sdp_probe", "OFF (default); no SDP hook installed")',OBSERVER)
    def test_exact_build_and_prologue_signature_gated(self):
        self.assertIn('if (!uuid_ok)',OBSERVER)
        self.assertIn('0x001e8e78',OBSERVER)
        for signature in ('0xd503237f','0xa9bd57f6','0xa9014ff4','0xa9027bfd'):
            self.assertIn(signature,OBSERVER)
        self.assertIn('memcmp(current, SDP_RECORD_ADD_SIG',OBSERVER)
    def test_original_record_is_passed_through_once_and_sink_is_strictly_gated(self):
        # The existing/native record is always forwarded once without mutation.
        self.assertEqual(OBSERVER.count('g_sdp_orig(descriptor, handle_out)'),1)
        # The additional record is v8.2-only, never part of passive observation.
        self.assertEqual(OBSERVER.count('g_sdp_orig((uint64_t)(uintptr_t)candidate,'),1)
        self.assertIn('ret == 0 && service_hint == 0x110a && g_sink_armed &&',OBSERVER)
        self.assertIn('g_sink_accepted && g_audio_table_extended &&',OBSERVER)
        self.assertIn('__sync_bool_compare_and_swap(&g_sink_sdp_registered, 0u, 1u)',OBSERVER)
        self.assertIn('SinkSDPDescriptor()',OBSERVER)
        self.assertIn('remote SDP 0x110B NOT VERIFIED',OBSERVER)
        self.assertNotIn('g_orig[HOOK_REGISTER](',OBSERVER)
        self.assertNotIn('OI_SDPDB_AddServiceRecord(',OBSERVER)
    def test_bounded_and_safe_private_reads(self):
        self.assertIn('safe_peek(descriptor, header, sizeof header)',OBSERVER)
        self.assertIn('unsigned limit = count < 8u ? count : 8u;',OBSERVER)
        self.assertIn('count > 128u',OBSERVER)
        self.assertIn('n <= 24u || (n % 100u) == 0u',OBSERVER)
        self.assertIn('safe_peek(address + (uint64_t)i * 24u',OBSERVER)
    def test_unmodified_five_hook_baseline(self):
        sig=(ROOT/'tweak/BluetoothdBuild.h').read_text()
        self.assertIn('HOOK_PUBLISH, HOOK_COUNT',sig)
        self.assertIn('install_hooks((uintptr_t)mh, uuid_ok);',SRC)
        self.assertIn('install_optional_sdp_probe((uintptr_t)mh, uuid_ok);',SRC)
    def test_ui_plist_valid_without_switches(self):
        d=plistlib.loads((ROOT/'settings/Resources/Root.plist').read_bytes())
        self.assertEqual([x for x in d['items'] if x.get('cell')=='PSSwitchCell'],[])


# ---- registering the Sink after iOS's six Sources ----
# Source invariants only. NOT live Bluetooth verification.

S=(Path(__file__).resolve().parents[1]/'tweak/Hooks.xm').read_text()


class PostSourceTable(unittest.TestCase):
    def test_only_after_original_native_loop(self):
        block=S.split('static uint64_t hook_native_registration(',1)[1].split('static void install_native_registration_hook(',1)[0]
        self.assertLess(block.index('g_source_reg_orig(audio_context)'),block.index('register_sink_after_sources(audio_context)'))
    def test_registration_not_reentrant_inside_sixth_source(self):
        block=S.split('if (sources == 6u)',1)[1].split('if (SpeakaExperimentsEnabled()',1)[0]
        self.assertNotIn('register_sink_after_sources(',block)
        self.assertIn('snapshot_allocation();',block)
    def test_exact_hook_image(self):
        self.assertIn('0x003b4128ul',S)
        for x in ['0xd503237fu','0xd10103ffu','0xa90157f6u','0xa9024ff4u']:
            self.assertIn(x,S)
    def test_memory_and_gate(self):
        self.assertIn('calloc(slots, 32u)',S)   # v9.9.11: 7 (single) or 8 (dual) entries, checked against the allocation
        self.assertIn('calloc(1u, 3u * 16u + SINK_CODEC_TAIL_BYTES)',S)   # room for SBC (4) or AAC-LC (6)
        self.assertIn('0x31u, 0x15u, 0x02u, 0xfau',S)
        self.assertIn('memcpy(new_caps + 0x18u, &sink_codec_ptr',S)
        self.assertIn('memcpy((void *)(uintptr_t)(audio_context + 0x78u)',S)
        self.assertIn('audio_context + 0x68u',S)
        self.assertIn('audio_context + 0x6cu',S)
        self.assertIn('g_leaked_original_table',S)
        self.assertIn('g_signal_rx_hook_ok',S)
        self.assertIn('g_signal_tx_hook_ok',S)
        self.assertIn('g_sdp_hook_ok',S)
    def test_no_trick_acceptance(self):
        self.assertNotIn('SetConfig_accept_SBC returned 0',S)
        self.assertIn('V83 SEP RESULT',S)
        self.assertIn('V83 SDP add',S)


# ---- the media hook ----
# v8.7 static invariants only: NOT evidence of on-device media reception.

class MediaIngress(unittest.TestCase):
    def test_build(self):
        self.assertIn('v8.8-continuous-sbc-ring', S)
    def test_exact_verified_media_offset(self):
        self.assertIn('#define MEDIA_HANDLER_ENTRY 0x000d8cf8ul', S)
        self.assertIn('0xd503237fu, 0xd10203ffu, 0xa90367fau, 0xa9045ff8u', S)
    def test_no_structure_or_packet_read_in_hook(self):
        block=S.split('static uint64_t hook_receive_media(',1)[1].split('static void install_media_hook(',1)[0]
        self.assertIn('__sync_add_and_fetch(&g_media_calls, 1u)', block)
        self.assertIn('g_media_recent[(calls - 1u) & 7u]', block)
        self.assertIn('__atomic_store_n(&record->cid, raw_cid', block)
        self.assertIn('__atomic_store_n(&record->bytes, raw_bytes', block)
        self.assertNotIn('SpeakaLogLine', block)
        self.assertNotIn('SpeakaSetStatus', block)
        self.assertIn('g_media_orig(cid, packet, length, a3, a4, a5, a6, a7)',block)
        for forbidden in ['safe_peek(', 'g_v86_cid_lookup(', 'entry +', 'packet +', 'memcpy(', 'MSHookFunction(', 'g_sink_media_cid']:
            self.assertNotIn(forbidden, block)
    def test_no_helpers_or_patch_to_stub(self):
        block=S.split('/* v8.7: exact verified native L2CAP',1)[1].split('/* v8.3 one-shot:',1)[0]
        self.assertIn('MSHookFunction((void *)(base + MEDIA_HANDLER_ENTRY)',block)
        self.assertNotIn('SpeakaAudioHelper',block)
    def test_sink_requires_observer(self):
        block=S.split('static void register_sink_after_sources(',1)[1].split('static uint64_t hook_native_registration(',1)[0]
        self.assertIn('!g_media_hook_ok',block)
    def test_v85_sink_design_retained(self):
        for invariant in ['0x31u, 0x15u, 0x02u, 0xfau','V83 AUDIO_TABLE SWAPPED',
                          'V83 SEP RESULT', 'V83 SDP add ret=', 'V86 SINK_START_ACCEPT_QUEUED']:
            self.assertIn(invariant, S)


if __name__ == "__main__":
    unittest.main()
