/* AACPacket.h - turn one A2DP AAC media payload into AAC access units, whatever standard transport the source used.
 *
 * A2DP specifies LATM (RFC 3016). Sources differ in how they use it, and some use other standard framings, so each of
 * these is implemented and accepted only when its own length fields account for every byte of the packet:
 *   LATM      AudioMuxElement(1) elements (in-band StreamMuxConfig, persisted across packets), one or more per packet
 *   LOAS      AudioSyncStream: 0x2B7 sync + 13-bit length + AudioMuxElement(1), one or more per packet
 *   ADTS      0xFFF sync + frame_length, one or more frames per packet
 *   RFC3640   mpeg4-generic AAC-hbr: AU-headers-length, 13-bit AU-size headers, then the access units
 *   LATM_OOB  AudioMuxElement(0) elements: configuration out of band (the negotiated one)
 *   RAW       the payload is one bare access unit (tried last; checked against the AAC syntax's first element)
 * Each is also tried after a one-byte media payload header (the SBC-style frame count): accepted only if its count
 * matches the access units found. The transport that parses is remembered and tried first on the next packet.
 * Pure C, no OS calls. */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include "LATM.h"
#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    AAC_TRANSPORT_NONE = 0,
    AAC_TRANSPORT_LATM,
    AAC_TRANSPORT_LOAS,
    AAC_TRANSPORT_ADTS,
    AAC_TRANSPORT_RFC3640,
    AAC_TRANSPORT_LATM_OOB,
    AAC_TRANSPORT_RAW,
    AAC_TRANSPORT_COUNT
} AACTransport;

#define AAC_PACKET_MAX_AUS 16u

typedef struct {
    LATMConfig latm;          /* the in-band StreamMuxConfig, across packets */
    LATMConfig oob;           /* the out-of-band configuration (for LATM_OOB): one AU per element, no other data */
    AACTransport locked;      /* the transport the last good packet used; tried first */
    unsigned locked_header;   /* 1 if that packet had a one-byte media payload header */
    unsigned channels;        /* negotiated channels, for the raw access unit check */
} AACPacketState;

typedef struct {
    AACTransport transport;   /* what the packet was; NONE if nothing accounted for it */
    unsigned header;          /* 1 if a one-byte media payload header preceded it */
    unsigned aus;             /* access units extracted */
    unsigned asc_seen;        /* 1 if the packet carried a StreamMuxConfig (LATM/LOAS) */
    int latm_error;           /* when NONE: why LATM rejected it (LATM_E_*), and at which bit */
    size_t latm_error_bit;
    unsigned incomplete;      /* 1 if it looks like the first fragment of a larger element */
} AACPacketResult;

/* asc: the negotiated AudioSpecificConfig (2 bytes), used for the out-of-band configuration. */
void aac_packet_init(AACPacketState *st, const uint8_t asc[2], unsigned channels);

/* Split payload in[0..len) into access units, copied back to back into out (out_cap bytes), with their lengths in
 * au_len[0..]. Returns the number of access units (>= 1); 0 if the packet is incomplete (a fragment: append the
 * next one and try again); -1 if no transport accounts for it (res says why LATM refused it). */
int aac_packet_split(AACPacketState *st, const uint8_t *in, size_t len, uint8_t *out, size_t out_cap,
                     size_t *au_len, unsigned max_aus, AACPacketResult *res);

/* Forget the remembered transport, so the next packet is detected afresh (the decoder rejected what it gave). */
void aac_packet_unlock(AACPacketState *st);

const char *aac_transport_name(AACTransport t);

#ifdef __cplusplus
}
#endif
