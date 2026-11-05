"""v8.0 static safety gates and binary-signature provenance."""
from pathlib import Path
import plistlib
import unittest
import re

ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/'tweak/Hooks.xm').read_text()

class V80ConfigGetterObserver(unittest.TestCase):
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
        d=(ROOT/'docs/history/TESTING-v8.0.md').read_text()
        self.assertIn('No memory changes',d)
        self.assertIn('NO_INDEX3_CALL_OBSERVED',d)
        self.assertIn('Legacy Sink',d)

if __name__=='__main__':unittest.main()
