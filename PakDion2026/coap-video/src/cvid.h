/*
 * cvid.h -- shared definitions for the CoAP live-video experiment.
 *
 * Wire format of the /cam/frame and /cam/stream representations:
 *
 *     +----------------------+----------------------------+
 *     | cvid_hdr_t (28 byte) |  JPEG image (jpeg_len byte) |
 *     +----------------------+----------------------------+
 *
 * The header travels with the payload rather than in CoAP options so that the
 * representation is byte-identical no matter whether it is carried by RFC 7959
 * Block2 or RFC 9177 Q-Block2.  That keeps the two arms of the comparison
 * strictly payload-equivalent: any difference in the measurements comes from
 * the block-wise machinery alone.
 */
#ifndef CVID_H_
#define CVID_H_

#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <time.h>

#define CVID_MAGIC   0x44495643u /* "CVID" */
#define CVID_VERSION 1

/* Elective, no-cache-key option carrying the server's frame sequence so a
 * client can correlate a body with a capture without parsing the payload. */
#define CVID_OPT_FRAME_SEQ 65004

/* IANA CoAP Content-Format for image/jpeg; libcoap has no constant for it. */
#define CVID_MEDIATYPE_IMAGE_JPEG 23

typedef struct __attribute__((packed)) {
  uint32_t magic;      /* CVID_MAGIC, little endian on the wire            */
  uint8_t  version;    /* CVID_VERSION                                     */
  uint8_t  flags;      /* reserved, 0                                      */
  uint16_t width;
  uint16_t height;
  uint16_t reserved;
  uint32_t seq;        /* monotonically increasing frame counter           */
  uint64_t capture_us; /* CLOCK_REALTIME us when the frame left the camera */
  uint32_t jpeg_len;   /* octets of JPEG following this header             */
} cvid_hdr_t;

#define CVID_HDR_LEN ((size_t)28)
_Static_assert(sizeof(cvid_hdr_t) == CVID_HDR_LEN, "cvid_hdr_t must be 28 bytes");

/* ---- monotonic + wall clocks, microseconds ---- */

static inline uint64_t
cvid_now_mono_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

static inline uint64_t
cvid_now_real_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

/* ---- which block-wise mechanism is under test ---- */

typedef enum {
  CVID_RFC7959 = 0, /* Block1/Block2, lock-step, confirmable    */
  CVID_RFC9177 = 1  /* Q-Block1/Q-Block2, bursting, NON         */
} cvid_rfc_t;

static inline const char *
cvid_rfc_name(cvid_rfc_t r) {
  return r == CVID_RFC9177 ? "rfc9177" : "rfc7959";
}

/* Parse a seconds value like "2" or "2.5" into libcoap fixed point (n/1000). */
static inline void
cvid_parse_fixed(const char *arg, uint16_t *integer_part,
                 uint16_t *fractional_part) {
  double v = atof(arg);
  if (v < 0)
    v = 0;
  *integer_part = (uint16_t)v;
  *fractional_part = (uint16_t)((v - (double)*integer_part) * 1000.0 + 0.5);
}

/* Validate a received body and locate the JPEG inside it.  Returns 0 on
 * success and a negative value describing the first problem found. */
static inline int
cvid_body_check(const uint8_t *body, size_t len, cvid_hdr_t *out) {
  cvid_hdr_t h;
  if (len < CVID_HDR_LEN)
    return -1;
  memcpy(&h, body, CVID_HDR_LEN);
  if (h.magic != CVID_MAGIC)
    return -2;
  if (h.version != CVID_VERSION)
    return -3;
  if ((size_t)h.jpeg_len + CVID_HDR_LEN != len)
    return -4;
  if (h.jpeg_len < 4)
    return -5;
  /* JPEG must start with SOI and end with EOI or the body is torn. */
  if (body[CVID_HDR_LEN] != 0xFF || body[CVID_HDR_LEN + 1] != 0xD8)
    return -6;
  if (body[len - 2] != 0xFF || body[len - 1] != 0xD9)
    return -7;
  if (out)
    *out = h;
  return 0;
}

#endif /* CVID_H_ */
