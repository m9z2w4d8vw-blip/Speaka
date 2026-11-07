/*
 * shared/SpeakaShared.h - constants shared by the tweak (runs inside the Bluetooth daemon) and the Settings pane.
 *
 * The daemon runs as user `mobile` (its launchd plist says so), the same user as the Settings app, so a preference
 * value the daemon writes with kCFPreferencesCurrentUser is one Settings can read back.
 *
 * Log channels (the sandbox profile of the daemon is not readable, so we try several and report which work):
 *   1. preferences  - the daemon's OWN domain, "com.apple.bluetooth" (its sandbox is built around its own domains)
 *   2. file in /tmp/FTABHarvest/ - the one directory its entitlements explicitly allow it to write
 *   3. file in /var/mobile/Library/Logs/ - probably denied, but cheap to try
 *   4. a Darwin notification "state" word - a few bits of status, no file or preference access needed
 */
#pragma once
#include <stdint.h>

#define SPEAKA_PREFS_DOMAIN     "com.apple.bluetooth"
#define SPEAKA_KEY_LOG          "SpeakaLog"           /* array of strings: the newest log lines            */
#define SPEAKA_KEY_STATUS       "SpeakaStatus"        /* dictionary string -> string: self-test + hook state */
#define SPEAKA_KEY_SDP_PROBE    "SpeakaSDPProbe"   /* OFF by default; optional read-only hook */
#define SPEAKA_KEY_SEVEN_SLOTS_ONCE "SpeakaV81SevenOnce" /* OFF by default, consumed before use */
#define SPEAKA_KEY_CONFIG_OBSERVER "SpeakaV80ConfigLookupObserver" /* OFF: pass-through getter timing observer */
#define SPEAKA_KEY_EXPERIMENTS  "SpeakaExperiments"   /* boolean, flipped by the switch in the Settings pane */
#define SPEAKA_KEY_AAC          "SpeakaAAC"           /* legacy (experiment): ignored - AAC decoding is standard since v9.9.11 */
#define SPEAKA_KEY_AAC_SINK     "SpeakaAACSink"       /* legacy (v9.8.8x-9.8.99 experiment): ignored, removed by the app */
/* v9.9.11: the dual Sink layout's outcome - "pending" while one is being set up, "ok" once both endpoints registered.
 * A "pending" left by a launch that never finished means the next Start uses the single AAC endpoint ("failed"). */
#define SPEAKA_KEY_DUAL_SINK_STATE "SpeakaDualSinkState"

#define SPEAKA_LOG_DIR      "/tmp/FTABHarvest"
#define SPEAKA_LOG_FILE     "/tmp/FTABHarvest/Speaka.log"
#define SPEAKA_LOG_FILE_ALT     "/var/mobile/Library/Logs/Speaka.log"
#define SPEAKA_HELPER_LOG_FILE  "/tmp/FTABHarvest/BTPSAudioHelper-v88.log"  /* launchd StandardOutPath of the helper */

#define SPEAKA_NOTIFY_STATE     "com.parv.speaka.state"

/* ---- v8.9: one-tap Start/Stop and audio output selection ---------------------------------------------------
 * Start/Stop: Settings writes SPEAKA_KEY_SESSION_REQUEST and posts SPEAKA_NOTIFY_SESSION. The tweak inside bluetoothd
 * consumes the request, sets or clears the two existing one-shot switches and restarts its own process. The
 * relaunched daemon reports the result under SPEAKA_STATUS_SESSION, matched to the request by its nonce.
 * Output: Settings stores the mode and a "never play to" list; BTPSAudioHelper reads them, re-reads them when
 * SPEAKA_NOTIFY_OUTPUT is posted, and publishes what is playing where under SPEAKA_KEY_HELPER_STATUS. */
#define SPEAKA_KEY_SINK_ONCE "SpeakaV82DecisiveOnce"  /* same literal tweak/Hooks.xm consumes at launch */
#define SPEAKA_NOTIFY_SESSION    "com.parv.speaka.v89.session"
#define SPEAKA_KEY_SESSION_REQUEST       "SpeakaV89SessionRequest" /* {action,nonce,time}; consumed by the tweak */
#define SPEAKA_KEY_SESSION_PENDING       "SpeakaV89SessionPending" /* carried across the restart; consumed */
#define SPEAKA_STATUS_SESSION    "diag.v89.session"
#define SPEAKA_NOTIFY_OUTPUT     "com.parv.speaka.v89.output"
#define SPEAKA_KEY_OUTPUT_MODE   "SpeakaV89OutputMode"     /* "speaker" (default) | "headphones" | "any" */
#define SPEAKA_KEY_NEVER_OUTPUTS         "SpeakaV89NeverOutputs"   /* array of {uid,name}: never play there */
#define SPEAKA_KEY_HELPER_STATUS        "SpeakaV89HelperStatus"   /* written by BTPSAudioHelper for Settings */
/* v9.9.6: the app's verified name cache (its own defaults): iOS's name/address pairs, bounded (PeerName.h). */
#define SPEAKA_APP_KEY_PEER_NAMES "SpeakaPeerNames"

/* ---- the 64-bit notification state word ---------------------------------------------------------------------
 * bit 0  tweak loaded in the daemon      bit 1  UUID matched the analysed build     bit 2  experiments enabled
 * bits 8-15  hooks installed             bits 16-23  hooks skipped/failed           bits 32-63  last event (unix secs) */
static inline uint64_t speaka_state_pack(unsigned loaded, unsigned uuid_ok, unsigned experiments,
                                       unsigned hooks_ok, unsigned hooks_bad, uint32_t last_event)
{
    return (uint64_t)(loaded & 1u) | ((uint64_t)(uuid_ok & 1u) << 1) | ((uint64_t)(experiments & 1u) << 2) |
           ((uint64_t)(hooks_ok & 0xFFu) << 8) | ((uint64_t)(hooks_bad & 0xFFu) << 16) | ((uint64_t)last_event << 32);
}
static inline void speaka_state_unpack(uint64_t s, unsigned *loaded, unsigned *uuid_ok, unsigned *experiments,
                                     unsigned *hooks_ok, unsigned *hooks_bad, uint32_t *last_event)
{
    if (loaded)      *loaded      = (unsigned)(s & 1u);
    if (uuid_ok)     *uuid_ok     = (unsigned)((s >> 1) & 1u);
    if (experiments) *experiments = (unsigned)((s >> 2) & 1u);
    if (hooks_ok)    *hooks_ok    = (unsigned)((s >> 8) & 0xFFu);
    if (hooks_bad)   *hooks_bad   = (unsigned)((s >> 16) & 0xFFu);
    if (last_event)  *last_event  = (uint32_t)(s >> 32);
}

/* Opt-in AVRCP experiment; absent/off preserves current playback behavior. */
#define SPEAKA_KEY_VOLUME_MODE "SpeakaAVRCPVolumeMode"   /* legacy (v9.8.9x experiment): removed at install */
/* v9.9.11: AVRCP volume sync - a standard feature. Absent or "on": on. "off" / "observe": recovery and diagnostics
 * (BTPSAudioHelper --volume-mode). A new key, so the experiment's old values (off by default) don't carry over. */
#define SPEAKA_KEY_VOLUME_SYNC "SpeakaVolumeSync"
#define SPEAKA_KEY_VOLUME_REQUEST "SpeakaAVRCPLocalVolumeRequest"
/* The last verified Speaka PC (PeerIdentity.c): bound at startup so its AVRCP requests are answered even if they
 * arrive before its A2DP configuration. Written by the tweak. */
#define SPEAKA_KEY_VOLUME_PEER "SpeakaAVRCPVolumePeer"
/* The last volume that PC set (0..127), reported when it next registers - so a reconnection keeps its level.
 * Written by the tweak. */
#define SPEAKA_KEY_VOLUME_LEVEL "SpeakaAVRCPVolumeLevel"
/* v9.9.10: which volume Windows controls - "ios" (default: iOS output volume, one volume) or "speaka" (Speaka's own
 * PCM gain, no reverse sync). Written by BTPSAudioHelper --volume-domain. */
#define SPEAKA_KEY_VOLUME_DOMAIN "SpeakaAVRCPVolumeDomain"
