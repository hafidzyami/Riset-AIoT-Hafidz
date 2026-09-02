/*
 * coap_video_server.c -- live MJPEG video over CoAP block-wise transfer.
 *
 * Serves the camera as four resources:
 *
 *   GET /cam/frame   latest complete frame, one body per request (pull)
 *   GET /cam/stream  same representation, observable (RFC 7641 push)
 *   GET /cam/info    capture + block-wise configuration, text/plain
 *   GET /cam/stats   counters accumulated since start-up, text/plain
 *
 * The block-wise mechanism is chosen once at start-up with --rfc:
 *
 *   --rfc 7959   Block2 (RFC 7959): lock-step, one confirmable round trip per
 *                block, so a frame costs roughly N_blocks * RTT.
 *   --rfc 9177   Q-Block2 (RFC 9177): the whole body is burst out as
 *                non-confirmable payloads, MAX_PAYLOADS at a time, and only
 *                the blocks the client reports missing are resent.
 *
 * Everything runs in one thread: coap_io_process_with_fds() lets the camera
 * pipe share libcoap's select(), which keeps frame handling lock-free.
 */
#include "cvid.h"
#include "frame_source.h"

#include <coap3/coap.h>

#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <netdb.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* ------------------------------------------------------------------ state */

typedef struct {
  /* newest complete frame, already framed as cvid_hdr_t + JPEG */
  uint8_t  *body;
  size_t    body_len;
  uint32_t  seq;
  uint64_t  capture_us;

  coap_resource_t *r_stream;

  /* configuration */
  cvid_rfc_t rfc;
  uint16_t   block_size;
  uint16_t   max_payloads;
  uint16_t   nt_int, nt_frac;  /* NON_TIMEOUT */
  uint16_t   nrt_int, nrt_frac; /* NON_RECEIVE_TIMEOUT, 0 = library default */
  int        notify_con;
  fs_config_t cam;

  /* counters */
  uint64_t frames_captured, frames_served, bodies_bytes, requests;
  uint64_t frames_replaced;    /* overwritten before any client fetched them */
  int      body_served;        /* has the current body been handed out yet?  */
  uint64_t started_us;
} srv_t;

static srv_t g_srv;
static volatile sig_atomic_t g_quit = 0;

static void
on_signal(int sig) {
  (void)sig;
  g_quit = 1;
}

/* --------------------------------------------------------- frame handling */

/* Called once per complete JPEG out of rpicam-vid.  Re-frames the image with a
 * cvid header and replaces the "latest frame" slot. */
static void
on_frame(const uint8_t *jpeg, size_t len, void *arg) {
  srv_t *s = arg;
  size_t need = CVID_HDR_LEN + len;

  uint8_t *nb = malloc(need);
  if (!nb)
    return;

  cvid_hdr_t h;
  memset(&h, 0, sizeof(h));
  h.magic      = CVID_MAGIC;
  h.version    = CVID_VERSION;
  h.width      = (uint16_t)s->cam.width;
  h.height     = (uint16_t)s->cam.height;
  h.seq        = ++s->seq;
  h.capture_us = cvid_now_real_us();
  h.jpeg_len   = (uint32_t)len;

  memcpy(nb, &h, CVID_HDR_LEN);
  memcpy(nb + CVID_HDR_LEN, jpeg, len);

  /* A capture that is overwritten before any client asked for it never made it
   * off the device -- that is the camera outrunning the transport, which is
   * exactly what the slower block-wise mechanism causes. */
  if (s->body && !s->body_served)
    s->frames_replaced++;
  s->body_served = 0;

  free(s->body);
  s->body       = nb;
  s->body_len   = need;
  s->capture_us = h.capture_us;
  s->frames_captured++;

  /* Push to observers.  With RFC 9177 the notification body is burst out as
   * Q-Block2 payloads; with RFC 7959 each observer walks it block by block. */
  if (s->r_stream)
    coap_resource_notify_observers(s->r_stream, NULL);
}

/* ------------------------------------------------------------- resources  */

/* libcoap calls this once the last block of a body has been delivered. */
static void
release_snapshot(coap_session_t *session, void *app_ptr) {
  (void)session;
  free(app_ptr);
}

/*
 * Hand libcoap a private snapshot of the current frame.
 *
 * This is the crux of serving live video over block-wise transfer: a body must
 * not change underneath an in-flight transfer, or the client reassembles blocks
 * from two different frames into one corrupt image.  libcoap holds the buffer
 * until the last block is delivered and then calls release_func, so a malloc'd
 * copy per request gives every transfer a stable, self-consistent body.
 */
static void
serve_frame(coap_resource_t *resource, coap_session_t *session,
            const coap_pdu_t *request, const coap_string_t *query,
            coap_pdu_t *response) {
  srv_t *s = &g_srv;

  s->requests++;

  if (!s->body) {
    coap_pdu_set_code(response, COAP_RESPONSE_CODE_SERVICE_UNAVAILABLE);
    coap_add_data(response, 21, (const uint8_t *)"no frame captured yet");
    return;
  }

  uint8_t *snap = malloc(s->body_len);
  if (!snap) {
    coap_pdu_set_code(response, COAP_RESPONSE_CODE_INTERNAL_ERROR);
    return;
  }
  memcpy(snap, s->body, s->body_len);
  size_t snap_len = s->body_len;
  uint32_t seq = s->seq;

  coap_pdu_set_code(response, COAP_RESPONSE_CODE_CONTENT);

  /* Frame sequence as an elective option, so a client can log which capture a
   * body belongs to without decoding the payload. */
  uint8_t seqbuf[4];
  seqbuf[0] = (uint8_t)(seq >> 24);
  seqbuf[1] = (uint8_t)(seq >> 16);
  seqbuf[2] = (uint8_t)(seq >> 8);
  seqbuf[3] = (uint8_t)seq;
  coap_add_option(response, CVID_OPT_FRAME_SEQ, sizeof(seqbuf), seqbuf);

  /* ETag = frame sequence: lets libcoap and the client tell one capture from
   * the next.  maxage 0 marks the representation as never cacheable. */
  /*
   * Ownership note: on every failure path coap_add_data_large_response() has
   * already invoked release_func on app_ptr before returning 0 (see the
   * "error:" / "error_released:" labels in libcoap's coap_block.c).  Freeing
   * snap here as well is a double free -- it showed up as a "double free or
   * corruption" abort under large Q-Block2 bursts, where send failures make
   * this path reachable.  So on failure, do nothing: the buffer is already
   * gone, and the response code libcoap set stands.
   */
  if (!coap_add_data_large_response(resource, session, request, response,
                                    query, CVID_MEDIATYPE_IMAGE_JPEG,
                                    0 /* maxage: live data */,
                                    (uint64_t)seq, snap_len, snap,
                                    release_snapshot, snap))
    return;

  s->frames_served++;
  s->body_served = 1;
  s->bodies_bytes += snap_len;
}

static void
serve_info(coap_resource_t *resource, coap_session_t *session,
           const coap_pdu_t *request, const coap_string_t *query,
           coap_pdu_t *response) {
  srv_t *s = &g_srv;
  char buf[768];
  int n = snprintf(buf, sizeof(buf),
                   "mechanism=%s\n"
                   "block_size=%u\n"
                   "max_payloads=%u\n"
                   "non_timeout_s=%u.%03u\n"
                   "capture=%dx%d@%dfps q=%d\n"
                   "libcoap=%s\n",
                   cvid_rfc_name(s->rfc), s->block_size, s->max_payloads,
                   s->nt_int, s->nt_frac, s->cam.width, s->cam.height,
                   s->cam.framerate, s->cam.quality,
                   coap_package_version());
  coap_pdu_set_code(response, COAP_RESPONSE_CODE_CONTENT);
  coap_add_data_large_response(resource, session, request, response, query,
                               COAP_MEDIATYPE_TEXT_PLAIN, -1, 0,
                               (size_t)n, (const uint8_t *)buf, NULL, NULL);
}

static void
serve_stats(coap_resource_t *resource, coap_session_t *session,
            const coap_pdu_t *request, const coap_string_t *query,
            coap_pdu_t *response) {
  srv_t *s = &g_srv;
  char buf[768];
  uint64_t up = cvid_now_mono_us() - s->started_us;
  int n = snprintf(buf, sizeof(buf),
                   "uptime_s=%.3f\n"
                   "frames_captured=%" PRIu64 "\n"
                   "frames_served=%" PRIu64 "\n"
                   "frames_replaced=%" PRIu64 "\n"
                   "requests=%" PRIu64 "\n"
                   "body_bytes=%" PRIu64 "\n"
                   "capture_fps=%.2f\n"
                   "latest_seq=%u\n"
                   "latest_body_bytes=%zu\n",
                   up / 1e6, s->frames_captured, s->frames_served,
                   s->frames_replaced, s->requests, s->bodies_bytes,
                   up ? s->frames_captured * 1e6 / up : 0.0,
                   s->seq, s->body_len);
  coap_pdu_set_code(response, COAP_RESPONSE_CODE_CONTENT);
  coap_add_data_large_response(resource, session, request, response, query,
                               COAP_MEDIATYPE_TEXT_PLAIN, -1, 0,
                               (size_t)n, (const uint8_t *)buf, NULL, NULL);
}

/* -------------------------------------------------------- RFC 9177 tuning */

/*
 * MAX_PAYLOADS / NON_TIMEOUT are per-session in libcoap, and a server session
 * only exists once a client shows up -- so apply them from the event handler.
 */
static int
on_event(coap_session_t *session, coap_event_t event) {
  srv_t *s = &g_srv;

  if (event == COAP_EVENT_SERVER_SESSION_NEW && s->rfc == CVID_RFC9177) {
    coap_session_set_max_payloads(session, s->max_payloads);
    coap_session_set_non_timeout(
        session, (coap_fixed_point_t){s->nt_int, s->nt_frac});
    if (s->nrt_int || s->nrt_frac)
      coap_session_set_non_receive_timeout(
          session, (coap_fixed_point_t){s->nrt_int, s->nrt_frac});
    coap_log_info("RFC9177 session tuning: MAX_PAYLOADS=%u NON_TIMEOUT=%u.%03us\n",
                  s->max_payloads, s->nt_int, s->nt_frac);
  }
  return 0;
}

/* ----------------------------------------------------------------- main   */

static void
usage(const char *p) {
  fprintf(stderr,
    "usage: %s [options]\n"
    "\nCoAP block-wise mechanism:\n"
    "  --rfc 7959|9177     block-wise mechanism to serve with (default 7959)\n"
    "  --block-size N      16..1024, power of two (default 1024)\n"
    "  --max-payloads N    RFC 9177 MAX_PAYLOADS (default 10)\n"
    "  --non-timeout S     RFC 9177 NON_TIMEOUT seconds, fractional ok (def 2)\n"
    "  --force-q-block     skip Q-Block capability probing\n"
    "  --notify-con        send observe notifications confirmable\n"
    "\nCapture:\n"
    "  --width N --height N --framerate N --quality N\n"
    "                      default 640x480@15fps quality 80\n"
    "  --hflip --vflip --rotation N --denoise-off\n"
    "\nOther:\n"
    "  --port N            listen port (default 5683)\n"
    "  --addr A            bind address (default 0.0.0.0)\n"
    "  --loss SPEC         emulate transmit loss, e.g. 5%% (testing only)\n"
    "  -v N                libcoap log level 0..9\n", p);
}

int
main(int argc, char **argv) {
  srv_t *s = &g_srv;
  const char *addr = "0.0.0.0", *port = "5683", *loss = NULL;
  int log_level = COAP_LOG_WARN;
  int force_q = 0;
  frame_source_t fs;
  coap_context_t *ctx = NULL;
  int cam_open = 0;
  int rc = 1;

  memset(s, 0, sizeof(*s));
  s->rfc          = CVID_RFC7959;
  s->block_size   = 1024;
  s->max_payloads = 10;   /* RFC 9177 default */
  s->nt_int = 2;         /* RFC 9177 default NON_TIMEOUT */
  s->cam.width = 640;
  s->cam.height = 480;
  s->cam.framerate = 15;
  s->cam.quality = 80;

  enum { O_RFC = 1000, O_BS, O_MP, O_NT, O_FQ, O_NCON, O_W, O_H, O_FR, O_Q,
         O_HF, O_VF, O_ROT, O_DN, O_PORT, O_ADDR, O_LOSS, O_NRT };
  static const struct option lo[] = {
    {"rfc",           required_argument, 0, O_RFC},
    {"block-size",    required_argument, 0, O_BS},
    {"max-payloads",  required_argument, 0, O_MP},
    {"non-timeout",   required_argument, 0, O_NT},
    {"non-receive-timeout", required_argument, 0, O_NRT},
    {"force-q-block", no_argument,       0, O_FQ},
    {"notify-con",    no_argument,       0, O_NCON},
    {"width",         required_argument, 0, O_W},
    {"height",        required_argument, 0, O_H},
    {"framerate",     required_argument, 0, O_FR},
    {"quality",       required_argument, 0, O_Q},
    {"hflip",         no_argument,       0, O_HF},
    {"vflip",         no_argument,       0, O_VF},
    {"rotation",      required_argument, 0, O_ROT},
    {"denoise-off",   no_argument,       0, O_DN},
    {"port",          required_argument, 0, O_PORT},
    {"addr",          required_argument, 0, O_ADDR},
    {"loss",          required_argument, 0, O_LOSS},
    {"help",          no_argument,       0, 'h'},
    {0, 0, 0, 0}
  };

  int c;
  while ((c = getopt_long(argc, argv, "v:h", lo, NULL)) != -1) {
    switch (c) {
    case O_RFC:
      if (!strcmp(optarg, "7959"))
        s->rfc = CVID_RFC7959;
      else if (!strcmp(optarg, "9177"))
        s->rfc = CVID_RFC9177;
      else {
        fprintf(stderr, "--rfc must be 7959 or 9177\n");
        return 1;
      }
      break;
    case O_BS:   s->block_size   = (uint16_t)atoi(optarg); break;
    case O_MP:   s->max_payloads = (uint16_t)atoi(optarg); break;
    case O_NT:   cvid_parse_fixed(optarg, &s->nt_int, &s->nt_frac); break;
    case O_NRT:  cvid_parse_fixed(optarg, &s->nrt_int, &s->nrt_frac); break;
    case O_FQ:   force_q = 1; break;
    case O_NCON: s->notify_con = 1; break;
    case O_W:    s->cam.width     = atoi(optarg); break;
    case O_H:    s->cam.height    = atoi(optarg); break;
    case O_FR:   s->cam.framerate = atoi(optarg); break;
    case O_Q:    s->cam.quality   = atoi(optarg); break;
    case O_HF:   s->cam.hflip = 1; break;
    case O_VF:   s->cam.vflip = 1; break;
    case O_ROT:  s->cam.rotation = atoi(optarg); break;
    case O_DN:   s->cam.denoise_off = 1; break;
    case O_PORT: port = optarg; break;
    case O_ADDR: addr = optarg; break;
    case O_LOSS: loss = optarg; break;
    case 'v':    log_level = atoi(optarg); break;
    case 'h':    usage(argv[0]); return 0;
    default:     usage(argv[0]); return 1;
    }
  }

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  signal(SIGPIPE, SIG_IGN);

  coap_startup();
  coap_set_log_level(log_level);

  ctx = coap_new_context(NULL);
  if (!ctx) {
    fprintf(stderr, "cannot create CoAP context\n");
    goto out;
  }

  /*
   * Select the block-wise mechanism.
   *
   * COAP_BLOCK_USE_LIBCOAP   let libcoap drive the block state machine
   * COAP_BLOCK_SINGLE_BODY   reassemble bodies before handing them up
   * COAP_BLOCK_TRY_Q_BLOCK   offer RFC 9177 Q-Block, falling back to RFC 7959
   *                          if the peer rejects the option
   * COAP_BLOCK_USE_M_Q_BLOCK use the M bit when recovering Q-Block2, so one
   *                          request can cover a whole missing tail
   */
  uint32_t bm = COAP_BLOCK_USE_LIBCOAP | COAP_BLOCK_SINGLE_BODY;
  if (s->rfc == CVID_RFC9177) {
    bm |= COAP_BLOCK_TRY_Q_BLOCK | COAP_BLOCK_USE_M_Q_BLOCK;
    if (force_q)
      bm |= COAP_BLOCK_FORCE_Q_BLOCK;
  }
  coap_context_set_block_mode(ctx, bm);

  if (!coap_context_set_max_block_size(ctx, s->block_size)) {
    fprintf(stderr, "bad --block-size %u (need power of two, 16..1024)\n",
            s->block_size);
    goto out;
  }

  coap_register_event_handler(ctx, on_event);

  if (loss && !coap_debug_set_packet_loss(loss)) {
    fprintf(stderr, "bad --loss spec %s\n", loss);
    goto out;
  }

  /* listen */
  {
    coap_addr_info_t *info_list, *info;
    coap_str_const_t caddr = {strlen(addr), (const uint8_t *)addr};
    int bound = 0;

    info_list = coap_resolve_address_info(&caddr, (uint16_t)atoi(port), 0, 0, 0,
                                          AI_PASSIVE | AI_NUMERICHOST,
                                          1 << COAP_URI_SCHEME_COAP,
                                          COAP_RESOLVE_TYPE_LOCAL);
    for (info = info_list; info; info = info->next) {
      if (coap_new_endpoint(ctx, &info->addr, COAP_PROTO_UDP))
        bound = 1;
    }
    coap_free_address_info(info_list);
    if (!bound) {
      fprintf(stderr, "cannot bind %s:%s\n", addr, port);
      goto out;
    }
  }

  /* resources */
  {
    coap_resource_t *r;
    int nflags = s->notify_con ? COAP_RESOURCE_FLAGS_NOTIFY_CON
                               : COAP_RESOURCE_FLAGS_NOTIFY_NON;

    r = coap_resource_init(coap_make_str_const("cam/frame"), 0);
    coap_register_request_handler(r, COAP_REQUEST_GET, serve_frame);
    coap_add_attr(r, coap_make_str_const("ct"), coap_make_str_const("23"), 0);
    coap_add_resource(ctx, r);

    r = coap_resource_init(coap_make_str_const("cam/stream"), nflags);
    coap_register_request_handler(r, COAP_REQUEST_GET, serve_frame);
    coap_resource_set_get_observable(r, 1);
    coap_add_attr(r, coap_make_str_const("ct"), coap_make_str_const("23"), 0);
    coap_add_attr(r, coap_make_str_const("obs"), NULL, 0);
    coap_add_resource(ctx, r);
    s->r_stream = r;

    r = coap_resource_init(coap_make_str_const("cam/info"), 0);
    coap_register_request_handler(r, COAP_REQUEST_GET, serve_info);
    coap_add_resource(ctx, r);

    r = coap_resource_init(coap_make_str_const("cam/stats"), 0);
    coap_register_request_handler(r, COAP_REQUEST_GET, serve_stats);
    coap_add_resource(ctx, r);
  }

  if (fs_open(&fs, &s->cam) < 0) {
    fprintf(stderr, "cannot start rpicam-vid: %s\n", strerror(errno));
    goto out;
  }
  cam_open = 1;

  s->started_us = cvid_now_mono_us();
  fprintf(stderr,
          "coap-video-server: %s block=%uB %dx%d@%dfps q=%d on %s:%s\n",
          cvid_rfc_name(s->rfc), s->block_size, s->cam.width, s->cam.height,
          s->cam.framerate, s->cam.quality, addr, port);
  if (s->rfc == CVID_RFC9177)
    fprintf(stderr, "  RFC9177: MAX_PAYLOADS=%u NON_TIMEOUT=%u.%03us%s\n",
            s->max_payloads, s->nt_int, s->nt_frac, force_q ? " (forced)" : "");
  fflush(stderr);

  /* Single-threaded event loop: the camera pipe rides along in libcoap's
   * select() so frames and CoAP traffic are handled by the same thread. */
  while (!g_quit) {
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fs.fd, &rfds);

    int spent = coap_io_process_with_fds(ctx, 20, fs.fd + 1, &rfds, NULL, NULL);
    if (spent < 0)
      break;

    if (FD_ISSET(fs.fd, &rfds)) {
      if (fs_pump(&fs, on_frame, s) < 0) {
        fprintf(stderr, "camera pipe closed\n");
        break;
      }
    }
  }

  fprintf(stderr,
          "\nshutting down: captured=%" PRIu64 " served=%" PRIu64
          " requests=%" PRIu64 " body_bytes=%" PRIu64 "\n",
          s->frames_captured, s->frames_served, s->requests, s->bodies_bytes);
  rc = 0;

out:
  if (cam_open)
    fs_close(&fs);
  free(s->body);
  if (ctx)
    coap_free_context(ctx);
  coap_cleanup();
  return rc;
}
