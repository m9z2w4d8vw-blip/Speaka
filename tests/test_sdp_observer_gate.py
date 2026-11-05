"""Static regression guarantees for the optional v7 private SDP observer."""
from pathlib import Path
import plistlib
import unittest

ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/'tweak/Hooks.xm').read_text(encoding='utf-8')
MARKER='/* --------------------------------------------------------------------------------------- v7 SDP observer'
OBSERVER=SRC.split(MARKER,1)[1].split('/* ------------------------------------------------------------------------------------------------ installing */',1)[0]

class OptionalSDPProbeTests(unittest.TestCase):
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

if __name__ == '__main__': unittest.main()
