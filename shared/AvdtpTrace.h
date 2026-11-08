/* AvdtpTrace.h - a readable line for each AVDTP signal (v9.9.13). Header-only C: the tweak uses it to log V113_AVDTP next
 * to its raw V82 lines, and the host tests compile it.
 *
 * The raw lines are exact but numeric (sig=1 msg=2, RX_RAW 12 01 d0 00 ...). This names each signal and its type, and
 * for a DISCOVER answer each endpoint and its role - the difference between an attempt that works and one that doesn't:
 * iOS starting A2DP itself discovers the computer's endpoints, finds Sources only, and disconnects (docs/CONNECT-TO.md).
 *
 * AVDTP signaling header (single packet): byte 0 = transaction label (bits 7-4), packet type (3-2), message type (1-0);
 * byte 1 = signal identifier (bits 5-0). A DISCOVER accept lists 2 bytes per endpoint: SEID (bits 7-2) and in use
 * (bit 1); media type (bits 7-4) and TSEP (bit 3: 0 Source, 1 Sink). */
#ifndef SPEAKA_AVDTP_TRACE_H
#define SPEAKA_AVDTP_TRACE_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

static inline const char *sp_avdtp_signal_name(unsigned sig) {
    switch (sig & 63u) {
    case 0x01: return "DISCOVER";
    case 0x02: return "GET_CAPABILITIES";
    case 0x03: return "SET_CONFIGURATION";
    case 0x04: return "GET_CONFIGURATION";
    case 0x05: return "RECONFIGURE";
    case 0x06: return "OPEN";
    case 0x07: return "START";
    case 0x08: return "CLOSE";
    case 0x09: return "SUSPEND";
    case 0x0A: return "ABORT";
    case 0x0B: return "SECURITY_CONTROL";
    case 0x0C: return "GET_ALL_CAPABILITIES";
    case 0x0D: return "DELAYREPORT";
    default: return "UNKNOWN";
    }
}

static inline const char *sp_avdtp_message_name(unsigned msg) {
    switch (msg & 3u) {
    case 0: return "command";
    case 1: return "general-reject";
    case 2: return "accept";
    default: return "reject";
    }
}

/* The endpoints in a DISCOVER accept, as "52:src 54:src 55:src" (":inuse" when in use; ":video", ":other" for non-audio).
 * Returns how many; *sinks gets how many are audio Sinks - what an iOS-started (Source) A2DP connection can use.
 * Returns -1 for anything that isn't a single-packet DISCOVER accept with whole endpoint records. */
static inline int sp_avdtp_discover_endpoints(const uint8_t *pdu, size_t len, char *out, size_t outn, int *sinks) {
    if (out && outn) out[0] = 0;
    if (sinks) *sinks = 0;
    if (!pdu || len < 2 || ((pdu[0] >> 2) & 3u) != 0 || (pdu[0] & 3u) != 2 || (pdu[1] & 63u) != 0x01) return -1;
    if ((len - 2) % 2 != 0) return -1;
    int count = 0, sink_count = 0;
    size_t used = 0;
    for (size_t i = 2; i + 1 < len; i += 2) {
        unsigned seid = (unsigned)(pdu[i] >> 2), in_use = (unsigned)((pdu[i] >> 1) & 1u);
        unsigned media = (unsigned)(pdu[i + 1] >> 4), sink = (unsigned)((pdu[i + 1] >> 3) & 1u);
        if (media == 0 && sink && !in_use) sink_count++;
        if (out && used < outn) {
            int w = snprintf(out + used, outn - used, "%s%u:%s%s%s", count ? " " : "", seid, sink ? "snk" : "src",
                             media == 0 ? "" : media == 1 ? ":video" : ":other", in_use ? ":inuse" : "");
            if (w > 0) used += (size_t)w < outn - used ? (size_t)w : outn - used - 1;
        }
        count++;
    }
    if (sinks) *sinks = sink_count;
    return count;
}

/* "V113_AVDTP from=pc DISCOVER accept endpoints=52:src 54:src 55:src audio_sinks=0" - `from` is who sent this PDU. A
 * command from the iPhone is iOS's own procedure; a command from the pc is the computer's. Returns the length written. */
static inline int sp_avdtp_trace_line(const uint8_t *pdu, size_t len, int from_phone, char *out, size_t outn) {
    if (!out || !outn) return 0;
    out[0] = 0;
    if (!pdu || len < 2) return snprintf(out, outn, "V113_AVDTP from=%s short pdu len=%zu", from_phone ? "iphone" : "pc", len);
    unsigned packet = (unsigned)((pdu[0] >> 2) & 3u), msg = (unsigned)(pdu[0] & 3u);
    if (packet != 0)    /* start/continue/end packets carry the signal elsewhere */
        return snprintf(out, outn, "V113_AVDTP from=%s fragmented packet type=%u", from_phone ? "iphone" : "pc", packet);
    unsigned sig = (unsigned)(pdu[1] & 63u);
    int n = snprintf(out, outn, "V113_AVDTP from=%s %s %s tx=%u", from_phone ? "iphone" : "pc", sp_avdtp_signal_name(sig),
                     sp_avdtp_message_name(msg), (unsigned)(pdu[0] >> 4));
    if (n < 0 || (size_t)n >= outn) return n;
    if (sig == 0x01 && msg == 2) {
        char endpoints[160];
        int sinks = 0;
        int count = sp_avdtp_discover_endpoints(pdu, len, endpoints, sizeof endpoints, &sinks);
        if (count >= 0)
            n += snprintf(out + n, outn - (size_t)n, " endpoints=%s audio_sinks=%d", count ? endpoints : "none", sinks);
    } else if (len >= 3 && msg == 0 && sig >= 0x02 && sig <= 0x0D && sig != 0x0B) {
        n += snprintf(out + n, outn - (size_t)n, " seid=%u", (unsigned)(pdu[2] >> 2));   /* the endpoint it's about */
    }
    return n;
}

#endif
