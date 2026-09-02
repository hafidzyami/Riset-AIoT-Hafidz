/*
 * coap_video_client.c -- receive the CoAP video stream and measure it.
 *
 * Two receive modes:
 *
 *   --mode pull      GET /cam/frame in a loop.  One frame is one request, so
 *                    every measurement maps onto exactly one block-wise
 *                    transfer.  This is the benchmark path.
 *   --mode observe   GET /cam/stream with Observe (RFC 7641); the server pushes
 *                    a notification per capture.  Closer to how one would
 *                    really stream, but frames arrive unsolicited, so the
 *                    "latency" column is the interval between consecutive
 *                    completed notifications rather than a transfer duration.
 *                    Use pull mode when comparing the two mechanisms.
 *
 * The block-wise mechanism under test is picked with --rfc, exactly mirroring
 * the server.  Per-frame records go to --csv; a summary goes to stderr.
 *
 * Wire cost is taken from /proc/net/snmp UDP counters rather than from libcoap,
 * so it counts real datagrams including retransmissions and recovery traffic.
 */
#include "cvid.h"

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

/* --------------------------------------------------------- UDP wire stats */

typedef struct {
  uint64_t in_datagrams, out_datagrams, in_errors, rcvbuf_errors;
} udpstat_t;

/* Parse the Udp: line pair out of /proc/net/snmp. */
static int
udpstat_read(udpstat_t *u) {
  FILE *f = fopen("/proc/net/snmp", "r");
  char hdr[1024], val[1024];
  int ok = 0;

  memset(u, 0, sizeof(*u));
  if (!f)
    return -1;
  while (fgets(hdr, sizeof(hdr), f)) {
    if (strncmp(hdr, "Udp:", 4) != 0)
      continue;
    if (!fgets(val, sizeof(val), f))
      break;
    /* header names and values are positionally aligned */
    char *hs = hdr + 4, *vs = val + 4;
    char *hsave = NULL, *vsave = NULL;
    for (;;) {
      char *hn = strtok_r(hs, " \t\n", &hsave);
      char *vn = strtok_r(vs, " \t\n", &vsave);
      hs = vs = NULL;
      if (!hn || !vn)
        break;
      uint64_t v = strtoull(vn, NULL, 10);
      if (!strcmp(hn, "InDatagrams"))       u->in_datagrams = v;
      else if (!strcmp(hn, "OutDatagrams")) u->out_datagrams = v;
      else if (!strcmp(hn, "InErrors"))     u->in_errors = v;
      else if (!strcmp(hn, "RcvbufErrors")) u->rcvbuf_errors = v;
    }
    ok = 1;
    break;
  }
  fclose(f);
  return ok ? 0 : -1;
}

/* --------------------------------------------------------------- state    */

typedef enum { MODE_PULL = 0, MODE_OBSERVE = 1 } mode_t_;

typedef struct {
  cvid_rfc_t rfc;
  mode_t_    mode;
  uint16_t   block_size;
  uint16_t   max_payloads;
  uint16_t   nt_int, nt_frac;  /* NON_TIMEOUT */
  uint16_t   nrt_int, nrt_frac;  /* NON_RECEIVE_TIMEOUT, 0 = library default */
  uint32_t   want_frames;
  uint32_t   warmup;
  const char *save_dir;
  FILE      *mjpeg;   /* concatenated JPEG stream for a live viewer */

  /* per-transfer bookkeeping */
  uint64_t   t_req_us;      /* when the request went out            */
  int        pending;       /* a transfer is in flight              */
  int        got_response;  /* response handler fired for it        */
  uint8_t    token[8];      /* token of the request being timed      */
  size_t     token_len;

  /* results */
  uint32_t   n_ok, n_bad, n_timeout, n_recorded;
  uint64_t   bytes_ok;
  double    *lat_ms;        /* recorded latencies, want_frames long */
  uint32_t   lat_n;

  udpstat_t  udp0;
  uint64_t   t_start_us;
  FILE      *csv;
  uint32_t   last_seq;
  uint32_t   seq_gaps;      /* captures the client never saw        */
} cli_t;

static cli_t g_cli;
static volatile sig_atomic_t g_quit = 0;

static void
on_signal(int sig) {
  (void)sig;
  g_quit = 1;
}

static int
cmp_double(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return (x > y) - (x < y);
}

static double
pct(double *v, uint32_t n, double p) {
  if (!n)
    return 0.0;
  double idx = p / 100.0 * (n - 1);
  uint32_t lo = (uint32_t)idx;
  uint32_t hi = lo + 1 < n ? lo + 1 : lo;
  double frac = idx - lo;
  return v[lo] * (1 - frac) + v[hi] * frac;
}

/* --------------------------------------------------------- response path  */

static void
record_frame(cli_t *c, const uint8_t *data, size_t len, uint64_t t_done) {
  double lat_ms = (t_done - c->t_req_us) / 1000.0;
  cvid_hdr_t h = {0};
  int chk = cvid_body_check(data, len, &h);
  uint32_t nblocks = c->block_size
                     ? (uint32_t)((len + c->block_size - 1) / c->block_size)
                     : 0;

  if (chk == 0) {
    c->n_ok++;
    c->bytes_ok += len;
    if (c->last_seq && h.seq > c->last_seq + 1)
      c->seq_gaps += h.seq - c->last_seq - 1;
    c->last_seq = h.seq;
  } else {
    c->n_bad++;
  }

  /* Warm-up transfers are executed but not counted, so the numbers exclude
   * camera spin-up and the first-request Q-Block capability probe. */
  if (c->n_ok + c->n_bad <= c->warmup)
    return;

  if (chk == 0 && c->lat_ms && c->lat_n < c->want_frames)
    c->lat_ms[c->lat_n++] = lat_ms;
  c->n_recorded++;

  if (c->csv)
    fprintf(c->csv, "%u,%s,%u,%zu,%u,%.3f,%u,%d,%" PRIu64 "\n",
            c->n_recorded, cvid_rfc_name(c->rfc), c->block_size, len,
            nblocks, lat_ms, chk == 0 ? h.seq : 0, chk,
            (t_done - c->t_start_us) / 1000);

  /* Feed a live viewer: a bare concatenation of JPEGs is a valid MJPEG
   * stream, so this can be piped straight into ffplay/mpv. */
  if (chk == 0 && c->mjpeg) {
    fwrite(data + CVID_HDR_LEN, 1, h.jpeg_len, c->mjpeg);
    fflush(c->mjpeg);
  }

  if (chk == 0 && c->save_dir) {
    char path[512];
    snprintf(path, sizeof(path), "%s/frame_%06u.jpg", c->save_dir, h.seq);
    FILE *f = fopen(path, "wb");
    if (f) {
      fwrite(data + CVID_HDR_LEN, 1, h.jpeg_len, f);
      fclose(f);
    }
  }
}

static coap_response_t
on_response(coap_session_t *session, const coap_pdu_t *sent,
            const coap_pdu_t *received, const coap_mid_t mid) {
  cli_t *c = &g_cli;
  uint64_t t_done = cvid_now_mono_us();
  const uint8_t *data;
  size_t len;

  (void)session;
  (void)sent;
  (void)mid;

  /*
   * Ignore a response that does not belong to the request currently being
   * timed.  After a frame times out the loop moves on and issues a new
   * request; if the abandoned transfer then completes late, timing it against
   * the new request would record a bogus (tiny) latency and retire the new
   * request early.  Matching on the token keeps late arrivals out of the
   * statistics -- which matters precisely in the lossy runs, where timeouts
   * are common.  Observe notifications share the registration token and so
   * still match.
   */
  if (c->mode == MODE_PULL && c->token_len) {
    coap_bin_const_t tok = coap_pdu_get_token(received);
    if (tok.length != c->token_len ||
        memcmp(tok.s, c->token, c->token_len) != 0)
      return COAP_RESPONSE_OK;
  }

  coap_pdu_code_t code = coap_pdu_get_code(received);
  if (code != COAP_RESPONSE_CODE_CONTENT) {
    /* 2.31 Continue and friends are consumed inside libcoap; anything that
     * reaches here with a non-2.05 code is a real failure for this frame. */
    c->n_bad++;
    c->pending = 0;
    c->got_response = 1;
    return COAP_RESPONSE_OK;
  }

  /* With COAP_BLOCK_SINGLE_BODY libcoap only calls us once the whole body has
   * been reassembled, whichever block-wise mechanism carried it. */
  if (coap_get_data(received, &len, &data))
    record_frame(c, data, len, t_done);

  c->pending = 0;
  c->got_response = 1;
  return COAP_RESPONSE_OK;
}

static void
on_nack(coap_session_t *session, const coap_pdu_t *sent,
        const coap_nack_reason_t reason, const coap_mid_t mid) {
  cli_t *c = &g_cli;
  (void)session;
  (void)sent;
  (void)mid;

  if (reason == COAP_NACK_TOO_MANY_RETRIES ||
      reason == COAP_NACK_NOT_DELIVERABLE ||
      reason == COAP_NACK_RST) {
    c->n_timeout++;
    c->pending = 0;
    c->got_response = 1;
  }
}

/* ----------------------------------------------------------------- main   */

static void
usage(const char *p) {
  fprintf(stderr,
    "usage: %s --server ADDR [options]\n"
    "\nCoAP block-wise mechanism:\n"
    "  --rfc 7959|9177     mechanism under test (default 7959)\n"
    "  --block-size N      16..1024, power of two (default 1024)\n"
    "  --max-payloads N    RFC 9177 MAX_PAYLOADS (default 10)\n"
    "  --non-timeout S     RFC 9177 NON_TIMEOUT seconds, fractional ok (def 2)\n"
    "  --force-q-block     skip Q-Block capability probing\n"
    "\nRun:\n"
    "  --mode pull|observe receive mode (default pull)\n"
    "  --frames N          frames to record after warm-up (default 100)\n"
    "  --warmup N          transfers to discard first (default 5)\n"
    "  --timeout S         give up on a frame after S seconds (default 10)\n"
    "  --csv FILE          per-frame CSV output\n"
    "  --save-dir DIR      write received JPEGs there\n"
    "  --loss SPEC         emulate receive loss, e.g. 5%% (testing only)\n"
    "  --port N            server port (default 5683)\n"
    "  -v N                libcoap log level 0..9\n", p);
}

int
main(int argc, char **argv) {
  cli_t *c = &g_cli;
  const char *server = NULL, *port = "5683", *csv_path = NULL;
  const char *loss = NULL, *drop = NULL, *mjpeg_path = NULL;
  int log_level = COAP_LOG_WARN;
  int force_q = 0;
  double timeout_s = 10.0;
  coap_context_t *ctx = NULL;
  coap_session_t *session = NULL;
  coap_optlist_t *optlist = NULL;
  int rc = 1;

  memset(c, 0, sizeof(*c));
  c->rfc = CVID_RFC7959;
  c->mode = MODE_PULL;
  c->block_size = 1024;
  c->max_payloads = 10;
  c->nt_int = 2;             /* RFC 9177 default NON_TIMEOUT */
  c->want_frames = 100;
  c->warmup = 5;

  enum { O_SRV = 1000, O_RFC, O_BS, O_MP, O_NT, O_FQ, O_MODE, O_FR, O_WU,
         O_TO, O_CSV, O_SAVE, O_LOSS, O_DROP, O_PORT, O_MJPEG, O_NRT };
  static const struct option lo[] = {
    {"server",        required_argument, 0, O_SRV},
    {"rfc",           required_argument, 0, O_RFC},
    {"block-size",    required_argument, 0, O_BS},
    {"max-payloads",  required_argument, 0, O_MP},
    {"non-timeout",   required_argument, 0, O_NT},
    {"non-receive-timeout", required_argument, 0, O_NRT},
    {"force-q-block", no_argument,       0, O_FQ},
    {"mode",          required_argument, 0, O_MODE},
    {"frames",        required_argument, 0, O_FR},
    {"warmup",        required_argument, 0, O_WU},
    {"timeout",       required_argument, 0, O_TO},
    {"csv",           required_argument, 0, O_CSV},
    {"save-dir",      required_argument, 0, O_SAVE},
    {"mjpeg-out",     required_argument, 0, O_MJPEG},
    {"loss",          required_argument, 0, O_LOSS},
    {"drop",          required_argument, 0, O_DROP},
    {"port",          required_argument, 0, O_PORT},
    {"help",          no_argument,       0, 'h'},
    {0, 0, 0, 0}
  };

  int opt;
  while ((opt = getopt_long(argc, argv, "v:h", lo, NULL)) != -1) {
    switch (opt) {
    case O_SRV: server = optarg; break;
    case O_RFC:
      if (!strcmp(optarg, "7959"))
        c->rfc = CVID_RFC7959;
      else if (!strcmp(optarg, "9177"))
        c->rfc = CVID_RFC9177;
      else {
        fprintf(stderr, "--rfc must be 7959 or 9177\n");
        return 1;
      }
      break;
    case O_BS:   c->block_size   = (uint16_t)atoi(optarg); break;
    case O_MP:   c->max_payloads = (uint16_t)atoi(optarg); break;
    case O_NT:   cvid_parse_fixed(optarg, &c->nt_int, &c->nt_frac); break;
    case O_NRT:  cvid_parse_fixed(optarg, &c->nrt_int, &c->nrt_frac); break;
    case O_FQ:   force_q = 1; break;
    case O_MODE:
      c->mode = !strcmp(optarg, "observe") ? MODE_OBSERVE : MODE_PULL;
      break;
    case O_FR:   c->want_frames = (uint32_t)strtoul(optarg, NULL, 10); break;
    case O_WU:   c->warmup      = (uint32_t)strtoul(optarg, NULL, 10); break;
    case O_TO:   timeout_s = atof(optarg); break;
    case O_CSV:  csv_path = optarg; break;
    case O_SAVE: c->save_dir = optarg; break;
    case O_MJPEG: mjpeg_path = optarg; break;
    case O_LOSS: loss = optarg; break;
    case O_DROP: drop = optarg; break;
    case O_PORT: port = optarg; break;
    case 'v':    log_level = atoi(optarg); break;
    case 'h':    usage(argv[0]); return 0;
    default:     usage(argv[0]); return 1;
    }
  }

  if (!server) {
    usage(argv[0]);
    return 1;
  }

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);

  c->lat_ms = calloc(c->want_frames + 1, sizeof(double));
  if (!c->lat_ms)
    return 1;

  if (csv_path) {
    c->csv = fopen(csv_path, "w");
    if (!c->csv) {
      fprintf(stderr, "cannot open %s: %s\n", csv_path, strerror(errno));
      goto out;
    }
    fprintf(c->csv, "n,mechanism,block_size,body_bytes,blocks,latency_ms,"
                    "frame_seq,check,t_rel_ms\n");
  }

  /* When the video goes to stdout the machine-readable SUMMARY must not, or it
   * would corrupt the MJPEG stream. */
  FILE *summary_fp = stdout;
  if (mjpeg_path) {
    if (!strcmp(mjpeg_path, "-")) {
      c->mjpeg = stdout;
      summary_fp = stderr;
    } else {
      c->mjpeg = fopen(mjpeg_path, "wb");
      if (!c->mjpeg) {
        fprintf(stderr, "cannot open %s: %s\n", mjpeg_path, strerror(errno));
        goto out;
      }
    }
  }

  coap_startup();
  coap_set_log_level(log_level);

  ctx = coap_new_context(NULL);
  if (!ctx) {
    fprintf(stderr, "cannot create CoAP context\n");
    goto out;
  }

  uint32_t bm = COAP_BLOCK_USE_LIBCOAP | COAP_BLOCK_SINGLE_BODY;
  if (c->rfc == CVID_RFC9177) {
    bm |= COAP_BLOCK_TRY_Q_BLOCK | COAP_BLOCK_USE_M_Q_BLOCK;
    if (force_q)
      bm |= COAP_BLOCK_FORCE_Q_BLOCK;
  }
  coap_context_set_block_mode(ctx, bm);

  if (!coap_context_set_max_block_size(ctx, c->block_size)) {
    fprintf(stderr, "bad --block-size %u\n", c->block_size);
    goto out;
  }

  coap_register_response_handler(ctx, on_response);
  coap_register_nack_handler(ctx, on_nack);

  if (loss && !coap_debug_set_packet_loss(loss)) {
    fprintf(stderr, "bad --loss spec %s\n", loss);
    goto out;
  }
  if (drop && !coap_debug_set_packet_drop(drop)) {
    fprintf(stderr, "bad --drop spec %s\n", drop);
    goto out;
  }

  /* connect */
  coap_address_t dst;
  {
    coap_addr_info_t *info_list;
    coap_str_const_t caddr = {strlen(server), (const uint8_t *)server};

    info_list = coap_resolve_address_info(&caddr, (uint16_t)atoi(port), 0, 0, 0,
                                          0, 1 << COAP_URI_SCHEME_COAP,
                                          COAP_RESOLVE_TYPE_REMOTE);
    if (!info_list) {
      fprintf(stderr, "cannot resolve %s\n", server);
      goto out;
    }
    dst = info_list->addr;
    coap_free_address_info(info_list);

    session = coap_new_client_session(ctx, NULL, &dst, COAP_PROTO_UDP);
    if (!session) {
      fprintf(stderr, "cannot create session to %s:%s\n", server, port);
      goto out;
    }
  }

  /*
   * RFC 9177 transmission parameters.  MAX_PAYLOADS bounds how many
   * non-confirmable payloads may be sent back to back before the sender must
   * pause for NON_TIMEOUT -- this is what replaces the per-block acknowledgement
   * of RFC 7959 as the congestion control mechanism.
   */
  if (c->rfc == CVID_RFC9177) {
    coap_session_set_max_payloads(session, c->max_payloads);
    coap_session_set_non_timeout(
        session, (coap_fixed_point_t){c->nt_int, c->nt_frac});
    if (c->nrt_int || c->nrt_frac)
      coap_session_set_non_receive_timeout(
          session, (coap_fixed_point_t){c->nrt_int, c->nrt_frac});
  }

  /*
   * Request options, built once and re-added to every request PDU.
   *
   * Uri-Path must be one option per path segment, so let libcoap derive the
   * options from a real URI rather than hand-rolling the split.
   */
  const char *path = (c->mode == MODE_OBSERVE) ? "cam/stream" : "cam/frame";
  {
    char uri_str[512];
    coap_uri_t uri;

    snprintf(uri_str, sizeof(uri_str), "coap://%s:%s/%s", server, port, path);
    if (coap_split_uri((const uint8_t *)uri_str, strlen(uri_str), &uri) < 0) {
      fprintf(stderr, "cannot parse %s\n", uri_str);
      goto out;
    }
    if (!coap_uri_into_optlist(&uri, &dst, &optlist, 1)) {
      fprintf(stderr, "cannot build options for %s\n", uri_str);
      goto out;
    }
  }

  udpstat_read(&c->udp0);
  c->t_start_us = cvid_now_mono_us();

  fprintf(stderr, "coap-video-client: %s mode=%s block=%uB frames=%u -> %s:%s\n",
          cvid_rfc_name(c->rfc), c->mode == MODE_OBSERVE ? "observe" : "pull",
          c->block_size, c->want_frames, server, port);
  if (c->rfc == CVID_RFC9177)
    fprintf(stderr,
            "  RFC9177: MAX_PAYLOADS=%u NON_TIMEOUT=%u.%03us "
            "NON_RECEIVE_TIMEOUT=%u.%03us%s\n",
            c->max_payloads, c->nt_int, c->nt_frac,
            coap_session_get_non_receive_timeout(session).integer_part,
            coap_session_get_non_receive_timeout(session).fractional_part,
            force_q ? " (forced)" : "");

  if (c->mode == MODE_OBSERVE) {
    /* One Observe registration, then sit and let notifications arrive. */
    coap_pdu_t *pdu = coap_pdu_init(COAP_MESSAGE_CON, COAP_REQUEST_CODE_GET,
                                    coap_new_message_id(session),
                                    coap_session_max_pdu_size(session));
    uint8_t obs = COAP_OBSERVE_ESTABLISH;
    coap_add_option(pdu, COAP_OPTION_OBSERVE, 1, &obs);
    coap_add_optlist_pdu(pdu, &optlist);
    c->t_req_us = cvid_now_mono_us();
    if (coap_send(session, pdu) == COAP_INVALID_MID) {
      fprintf(stderr, "observe registration failed\n");
      goto out;
    }
    while (!g_quit && c->n_recorded < c->want_frames) {
      if (coap_io_process(ctx, 100) < 0)
        break;
      /* Each notification is its own transfer; restart the latency clock so a
       * frame is timed from the moment the previous one completed. */
      if (c->got_response) {
        c->got_response = 0;
        c->t_req_us = cvid_now_mono_us();
      }
    }
  } else {
    while (!g_quit && c->n_recorded < c->want_frames) {
      /*
       * RFC 7959 uses confirmable requests: every block is acknowledged, so the
       * transfer is lock-step and self-clocked by the ACKs.
       * RFC 9177 is built around non-confirmable messages: the request goes out
       * NON and the server bursts the body back without waiting for per-block
       * acknowledgement.
       */
      coap_pdu_type_t ptype = (c->rfc == CVID_RFC9177) ? COAP_MESSAGE_NON
                                                       : COAP_MESSAGE_CON;
      coap_pdu_t *pdu = coap_pdu_init(ptype, COAP_REQUEST_CODE_GET,
                                      coap_new_message_id(session),
                                      coap_session_max_pdu_size(session));
      if (!pdu)
        break;

      uint8_t token[8];
      size_t tklen;
      coap_session_new_token(session, &tklen, token);
      coap_add_token(pdu, tklen, token);

      /* Remember it so on_response can discard late replies to an abandoned
       * transfer instead of timing them against this request. */
      c->token_len = tklen > sizeof(c->token) ? sizeof(c->token) : tklen;
      memcpy(c->token, token, c->token_len);
      coap_add_optlist_pdu(pdu, &optlist);

      c->t_req_us = cvid_now_mono_us();
      c->pending = 1;
      c->got_response = 0;

      if (coap_send(session, pdu) == COAP_INVALID_MID) {
        c->n_timeout++;
        c->pending = 0;
        continue;
      }

      uint64_t deadline = c->t_req_us + (uint64_t)(timeout_s * 1e6);
      while (!g_quit && c->pending) {
        if (coap_io_process(ctx, 50) < 0)
          break;
        if (cvid_now_mono_us() > deadline) {
          c->n_timeout++;
          c->n_recorded++;
          if (c->csv)
            fprintf(c->csv, "%u,%s,%u,0,0,%.3f,0,-99,%" PRIu64 "\n",
                    c->n_recorded, cvid_rfc_name(c->rfc), c->block_size,
                    timeout_s * 1000.0,
                    (cvid_now_mono_us() - c->t_start_us) / 1000);
          c->pending = 0;
        }
      }
    }
  }

  /* ------------------------------------------------------------ summary  */
  {
    uint64_t elapsed_us = cvid_now_mono_us() - c->t_start_us;
    udpstat_t u1;
    udpstat_read(&u1);
    uint64_t dg_in  = u1.in_datagrams  - c->udp0.in_datagrams;
    uint64_t dg_out = u1.out_datagrams - c->udp0.out_datagrams;
    double secs = elapsed_us / 1e6;

    qsort(c->lat_ms, c->lat_n, sizeof(double), cmp_double);

    double mean = 0;
    for (uint32_t i = 0; i < c->lat_n; i++)
      mean += c->lat_ms[i];
    if (c->lat_n)
      mean /= c->lat_n;

    fprintf(stderr,
      "\n===== %s / block %u B / %s =====\n"
      "recorded frames    : %u  (ok %u, corrupt %u, timeout %u)\n"
      "wall time          : %.2f s\n"
      "frame rate         : %.2f fps\n"
      "goodput            : %.3f Mbit/s  (%.1f KiB/s)\n"
      "latency ms         : mean %.1f  p50 %.1f  p95 %.1f  p99 %.1f  max %.1f\n"
      "UDP datagrams      : rx %" PRIu64 "  tx %" PRIu64 "  total %" PRIu64 "\n"
      "datagrams / frame  : %.1f\n"
      "captures skipped   : %u\n",
      cvid_rfc_name(c->rfc), c->block_size,
      c->mode == MODE_OBSERVE ? "observe" : "pull",
      c->n_recorded, c->n_ok, c->n_bad, c->n_timeout,
      secs,
      secs > 0 ? c->n_recorded / secs : 0.0,
      secs > 0 ? c->bytes_ok * 8.0 / secs / 1e6 : 0.0,
      secs > 0 ? c->bytes_ok / secs / 1024.0 : 0.0,
      mean, pct(c->lat_ms, c->lat_n, 50), pct(c->lat_ms, c->lat_n, 95),
      pct(c->lat_ms, c->lat_n, 99), c->lat_n ? c->lat_ms[c->lat_n - 1] : 0.0,
      dg_in, dg_out, dg_in + dg_out,
      c->n_recorded ? (double)(dg_in + dg_out) / c->n_recorded : 0.0,
      c->seq_gaps);

    /* One machine-readable line for the benchmark harness to scrape. */
    fprintf(summary_fp,
           "SUMMARY mechanism=%s mode=%s block=%u frames=%u ok=%u bad=%u "
           "timeout=%u secs=%.3f fps=%.3f goodput_mbps=%.4f "
           "lat_mean=%.2f lat_p50=%.2f lat_p95=%.2f lat_p99=%.2f lat_max=%.2f "
           "dg_in=%" PRIu64 " dg_out=%" PRIu64 " dg_per_frame=%.2f "
           "bytes=%" PRIu64 " gaps=%u\n",
           cvid_rfc_name(c->rfc), c->mode == MODE_OBSERVE ? "observe" : "pull",
           c->block_size, c->n_recorded, c->n_ok, c->n_bad, c->n_timeout,
           secs, secs > 0 ? c->n_recorded / secs : 0.0,
           secs > 0 ? c->bytes_ok * 8.0 / secs / 1e6 : 0.0,
           mean, pct(c->lat_ms, c->lat_n, 50), pct(c->lat_ms, c->lat_n, 95),
           pct(c->lat_ms, c->lat_n, 99),
           c->lat_n ? c->lat_ms[c->lat_n - 1] : 0.0,
           dg_in, dg_out,
           c->n_recorded ? (double)(dg_in + dg_out) / c->n_recorded : 0.0,
           c->bytes_ok, c->seq_gaps);
    fflush(summary_fp);
  }
  rc = (c->n_ok > 0) ? 0 : 2;

out:
  if (optlist)
    coap_delete_optlist(optlist);
  if (session)
    coap_session_release(session);
  if (ctx)
    coap_free_context(ctx);
  coap_cleanup();
  if (c->csv)
    fclose(c->csv);
  if (c->mjpeg && c->mjpeg != stdout)
    fclose(c->mjpeg);
  free(c->lat_ms);
  return rc;
}
