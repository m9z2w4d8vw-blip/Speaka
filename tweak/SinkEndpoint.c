/* SinkEndpoint.c - the speaker (A2DP Sink) endpoint, as pure functions with no OS calls: its capability list,
 * the checks on bluetoothd's endpoint table and allocator before it gets a seventh slot, the seven-slot config copy,
 * the Audio Sink SDP record, and the names the log uses for codecs, endpoints and results. */
#include "Tweak.h"
#include <string.h>
#include <stdint.h>
#include <stddef.h>
#include "../shared/AACConfig.h"

/* ---- the capability list ---- */
int sink_prepare_caps(SinkCapabilities *dst,
                               const uint8_t raw[48], const uint8_t codec[4]) {
    if (!dst || !raw || !codec) return -1;
    /* Only mirror the three fully observed SBC capability categories. The
       16-byte records encode (category, ... , codec_type, length, pointer).
       Do not infer Sink capability support from this layout alone. */
    if (raw[0] != 0x01u || raw[16] != 0x07u || raw[18] != 0x00u ||
        raw[19] != 0x04u || raw[32] != 0x08u || raw[17] != 0x00u)
        return -2;
    /* Fail if the observed SBC codec data has no supported parameters. This
       checks for a nonzero advertised frequency/mode and block/subbands. */
    if (!codec[0] || !codec[1]) return -3;
    memset(dst, 0, sizeof *dst);
    memcpy(dst->caps_words, raw, 48u);
    memcpy(dst->codec_bytes, codec, 4u);
    uint64_t owned_ptr = (uint64_t)(uintptr_t)dst->codec_bytes;
    /* The codec descriptor begins at raw offset 16, its data pointer +8. */
    memcpy((uint8_t*)dst->caps_words + 24u, &owned_ptr, sizeof owned_ptr);
    return 0;
}

/* The codec record's bytes must hold the largest codec Speaka can register: AAC-LC's 6. Compile-time, so a smaller
 * codec_bytes can never build (v9.8.84 sized a similar buffer for SBC's 4 and wrote AAC's 6 into it). */
typedef char sink_caps_codec_bytes_fit_aac[(AAC_CSI_BYTES <= sizeof(((SinkCapabilities *)0)->codec_bytes)) ? 1 : -1];

int sink_caps_use_aac(SinkCapabilities *caps) {
    if (!caps) return -1;
    uint8_t *raw = (uint8_t *)caps->caps_words;
    /* Record 1 must still be the captured SBC Media Codec record (category 7, SBC, 4 codec bytes). */
    if (raw[16] != 0x07u || raw[18] != A2DP_CODEC_SBC || raw[19] != 4u) return -2;
    uint8_t aac[AAC_CSI_BYTES];
    aac_build_sink_capability(aac);
    memset(caps->codec_bytes, 0, sizeof caps->codec_bytes);
    memcpy(caps->codec_bytes, aac, sizeof aac);
    raw[18] = A2DP_CODEC_AAC;
    raw[19] = (uint8_t)AAC_CSI_BYTES;
    uint64_t owned_ptr = (uint64_t)(uintptr_t)caps->codec_bytes;
    memcpy(raw + 24u, &owned_ptr, sizeof owned_ptr);    /* record 1's codec pointer, as sink_prepare_caps sets it */
    return 0;
}

/* v9.3 experimental, AAC: the protocol-level media-codec capability record for AAC-LC. Pure data from the public
 * A2DP AAC spec; advertises nothing by itself. */
size_t sink_build_aac_codec_record(uint8_t out[8]) {
    out[0] = 0x00u;                 /* media type: audio (AVDTP) */
    out[1] = A2DP_CODEC_AAC;        /* codec type: MPEG-2/4 AAC */
    aac_build_sink_capability(out + 2);
    return 8u;
}

/* ---- bluetoothd's endpoint table, before it grows ---- */
int sink_analyze_table(const uint8_t *slots, size_t bytes, unsigned count, SinkTableCapacity *out) {
    if (!out || !slots || count == 0 || count > SINK_TABLE_MAX_SLOTS ||
        bytes < (size_t)count * SINK_TABLE_SLOT_BYTES) return -1;
    SinkTableCapacity result;
    memset(&result, 0, sizeof result);
    result.total = count;
    for (unsigned i = 0; i < count; ++i) {
        uint8_t seid = slots[(size_t)i * SINK_TABLE_SLOT_BYTES + SINK_TABLE_SEID_OFFSET];
        result.seids[i] = seid;
        if (seid) {
            ++result.occupied;
            for (unsigned j = 0; j < i; ++j)
                if (result.seids[j] == seid) { ++result.duplicate_seids; break; }
        } else ++result.vacant;
    }
    *out = result;
    return 0;
}

/* ---- the allocator's geometry ---- */
int sink_analyze_allocation(const uint8_t config[3], const uint8_t context_counts[3],
                     unsigned pointer_present, SinkAllocation *out) {
    if (!out || !config || !context_counts || (pointer_present & ~7u)) return -1;
    /* The verified binary uses 8-bit counts. Reject nonsensical values rather
       than interpreting arbitrary bytes as a safe table size. */
    for (unsigned i = 0; i < 3; ++i)
        if (config[i] > SINK_MAX_COUNT || context_counts[i] > SINK_MAX_COUNT)
            return -2;
    if (context_counts[0] == 0u) return -3;
    SinkAllocation r;
    memset(&r, 0, sizeof r);
    const unsigned strides[3] = {
        SINK_ENDPOINT_STRIDE, SINK_SECONDARY_STRIDE, SINK_TERTIARY_STRIDE
    };
    r.config_matches_context = 1u;
    for (unsigned i = 0; i < 3; ++i) {
        r.configured[i] = config[i];
        r.observed[i] = context_counts[i];
        r.bytes[i] = (unsigned)context_counts[i] * strides[i];
        if (config[i] != context_counts[i]) r.config_matches_context = 0u;
        if (context_counts[i] && !(pointer_present & (1u << i)))
            r.missing_table_mask |= (1u << i);
    }
    *out = r;
    return 0;
}

/* ---- the seven-slot config copy ---- */
int sink_prepare_config_shadow(const uint8_t *source, size_t source_size,
                            uint8_t out[SINK_CONFIG_COPY_BYTES], int extra_slots)
{
    /* v9.9.11: one extra endpoint slot (the single AAC Sink) or two (AAC + SBC). Only byte 0 - the endpoint count the
     * initializer at +0xCE3F8 multiplies by the 40-byte slot stride and stores at +0x60 - changes. */
    if (!source || !out || source == out || source_size < SINK_CONFIG_COPY_BYTES ||
        extra_slots < 0 || extra_slots > 2) return -1;
    if (source[0] != 6u || source[1] != 3u || source[2] != 3u) return -2;
    memcpy(out, source, SINK_CONFIG_COPY_BYTES);
    out[0] = (uint8_t)(6 + extra_slots);
    return 0;
}

/* ---- the Audio Sink SDP record ---- */
/* Long-lived SDP objects. No pointers into a temporary stack array.
 * Attribute stride 24, element stride 16, descriptor two (pointer,count) pairs.
 * Derived from the binary Source arrays at +0x91ABD0, +0x91AC30,
 * +0x91AC50, primitive values at +0x773678..6D8, record add +0x1E8E78.
 * SDP publication is NOT proven until externally discovered over Bluetooth. */
typedef struct { uint8_t type, reserved; uint16_t length; uint32_t padding; uint64_t value; } Element;
typedef struct { uint16_t id; uint8_t padding[6]; Element element; } Attr;
typedef struct { const Attr *a; uint16_t count_a; uint8_t reserved_a[6];
                 const Attr *b; uint16_t count_b; uint8_t reserved_b[6]; } Descriptor;
#if defined(__cplusplus)
static_assert(sizeof(Element)==16 && sizeof(Attr)==24 && sizeof(Descriptor)==32, "private SDP layout changed");
#else
_Static_assert(sizeof(Element)==16 && sizeof(Attr)==24 && sizeof(Descriptor)==32, "private SDP layout changed");
#endif
#define U16(v) {3,0,4,0,(v)}
#define UINT16(v) {1,0,2,0,(v)}
#define SEQ(n,p) {6,0,(n),0,(uint64_t)(uintptr_t)(p)}
#define TXT(n,p) {4,0,(n),0,(uint64_t)(uintptr_t)(p)}
static const Element serviceClass[] = {U16(0x110Bu)};
static const Element l2capFields[] = {U16(0x0100u), UINT16(0x0019u)};
static const Element avdtpFields[] = {U16(0x0019u), UINT16(0x0103u)};
static const Element profileFields[] = {U16(0x110Du), UINT16(0x0103u)};
static const Element protocolList[] = {SEQ(2,l2capFields), SEQ(2,avdtpFields)};
static const Element profileList[] = {SEQ(2,profileFields)};
static const char sinkName[] = "Audio Sink";
static const Attr attributes[] = {
    {0x0001, {0}, SEQ(1,serviceClass)},
    {0x0004, {0}, SEQ(2,protocolList)},
    {0x0009, {0}, SEQ(1,profileList)},
    {0x0311, {0}, UINT16(0x0001u)}, /* A2DP Headphone supported feature */
};
static const Attr strings[] = {
    {0x0100, {0}, TXT(10,sinkName)},
};
static const Descriptor description = {attributes, 4, {0}, strings, 1, {0}};
const void *SinkSDPDescriptor(void) { return &description; }

/* ---- names for codecs, endpoints and results in the log ---- */
int avdtp_decode_caps(const uint8_t *buffer, size_t len, avdtp_caps_info_t *out)
{
    if (!out) return 0;
    memset(out, 0, sizeof *out);
    if (!buffer || !len || (len % AVDTP_CAP_RECORD_BYTES) ||
        len > AVDTP_CAP_RECORD_BYTES * AVDTP_CAP_MAX_RECORDS) return 0;

    uint32_t hash = 2166136261u;
    for (size_t i = 0; i < len; i++) hash = (hash ^ buffer[i]) * 16777619u;
    out->fingerprint = hash;
    out->total_records = (unsigned)(len / AVDTP_CAP_RECORD_BYTES);
    for (size_t off = 0; off < len; off += AVDTP_CAP_RECORD_BYTES) {
        switch (buffer[off]) {
            case 1: out->transport_records++; break;
            case 7:
                out->codec_records++;
                if (!out->codec_present) {
                    out->codec_present = 1;
                    out->media_type = buffer[off + 1];
                    out->codec_type = buffer[off + 2];
                    out->codec_data_len = buffer[off + 3];
                }
                break;
            case 8: out->delay_records++; break;
            default: out->unknown_records++; break;
        }
    }
    return 1;
}

const char *avdtp_codec_name(uint8_t type)
{
    switch (type) {
        case 0x00: return "SBC";
        case 0x01: return "MPEG-1/2 Audio";
        case 0x02: return "AAC";
        case 0x04: return "ATRAC";
        case 0xff: return "Vendor-specific";
        default: return "Unknown";
    }
}

const char *avdtp_endpoint_name(uint32_t media, uint32_t direction)
{
    if (media != 0) return "other-media";
    if (direction == 0) return "Audio Source";
    if (direction == 1) return "Audio Sink";
    return "unknown-direction";
}

const char *avdtp_registration_result(uint64_t result)
{
    if (result == 0) return "accepted";
    if (result == 0x68) return "0x68 (previously observed at endpoint exhaustion; meaning not ABI-verified)";
    return "nonzero (meaning not ABI-verified)";
}
