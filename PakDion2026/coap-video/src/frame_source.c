#include "frame_source.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define FS_READ_CHUNK   (64u * 1024u)
/* A single 1080p MJPEG frame measures ~70 kB; cap well above that so a stalled
 * reader can never grow the buffer without bound. */
#define FS_MAX_BUFFERED (8u * 1024u * 1024u)

static int
fs_reserve(frame_source_t *fs, size_t extra) {
  if (fs->len + extra <= fs->cap)
    return 0;
  size_t want = fs->cap ? fs->cap : FS_READ_CHUNK;
  while (want < fs->len + extra)
    want *= 2;
  uint8_t *p = realloc(fs->buf, want);
  if (!p)
    return -1;
  fs->buf = p;
  fs->cap = want;
  return 0;
}

int
fs_open(frame_source_t *fs, const fs_config_t *cfg) {
  int pipefd[2];
  char sw[16], sh[16], sfr[16], sq[16], srot[16];

  memset(fs, 0, sizeof(*fs));
  fs->cfg = *cfg;
  fs->fd = -1;

  if (pipe(pipefd) < 0)
    return -1;

  snprintf(sw, sizeof(sw), "%d", cfg->width);
  snprintf(sh, sizeof(sh), "%d", cfg->height);
  snprintf(sfr, sizeof(sfr), "%d", cfg->framerate);
  snprintf(sq, sizeof(sq), "%d", cfg->quality);
  snprintf(srot, sizeof(srot), "%d", cfg->rotation);

  pid_t pid = fork();
  if (pid < 0) {
    close(pipefd[0]);
    close(pipefd[1]);
    return -1;
  }

  if (pid == 0) {
    /* child: rpicam-vid -> stdout -> pipe */
    const char *argv[32];
    int n = 0;
    close(pipefd[0]);
    dup2(pipefd[1], STDOUT_FILENO);
    close(pipefd[1]);

    argv[n++] = "rpicam-vid";
    argv[n++] = "-t";          argv[n++] = "0";        /* run forever      */
    argv[n++] = "--codec";     argv[n++] = "mjpeg";
    argv[n++] = "--width";     argv[n++] = sw;
    argv[n++] = "--height";    argv[n++] = sh;
    argv[n++] = "--framerate"; argv[n++] = sfr;
    argv[n++] = "--quality";   argv[n++] = sq;
    argv[n++] = "--nopreview";
    argv[n++] = "--flush";                             /* low latency      */
    argv[n++] = "-o";          argv[n++] = "-";
    if (cfg->hflip)    argv[n++] = "--hflip";
    if (cfg->vflip)    argv[n++] = "--vflip";
    if (cfg->rotation) { argv[n++] = "--rotation"; argv[n++] = srot; }
    if (cfg->denoise_off) { argv[n++] = "--denoise"; argv[n++] = "cdn_off"; }
    argv[n] = NULL;

    /* rpicam-vid chatters on stderr once per frame; keep it out of our log. */
    int devnull = open("/dev/null", O_WRONLY);
    if (devnull >= 0) {
      dup2(devnull, STDERR_FILENO);
      close(devnull);
    }
    execvp(argv[0], (char *const *)argv);
    _exit(127);
  }

  close(pipefd[1]);
  fs->pid = pid;
  fs->fd = pipefd[0];
  return 0;
}

/* Locate the next FFD8 (SOI) at or after @p from. */
static ssize_t
find_soi(const uint8_t *b, size_t len, size_t from) {
  for (size_t i = from; i + 1 < len; i++)
    if (b[i] == 0xFF && b[i + 1] == 0xD8)
      return (ssize_t)i;
  return -1;
}

/* Locate the next FFD9 (EOI) at or after @p from.
 *
 * Safe to scan for naively: inside entropy-coded data a literal 0xFF is byte
 * stuffed as FF 00, so an unescaped FF D9 can only be a real end-of-image. */
static ssize_t
find_eoi(const uint8_t *b, size_t len, size_t from) {
  for (size_t i = from; i + 1 < len; i++)
    if (b[i] == 0xFF && b[i + 1] == 0xD9)
      return (ssize_t)(i + 2);
  return -1;
}

int
fs_pump(frame_source_t *fs, fs_frame_cb cb, void *arg) {
  int emitted = 0;

  if (fs_reserve(fs, FS_READ_CHUNK) < 0)
    return -1;

  ssize_t r = read(fs->fd, fs->buf + fs->len, fs->cap - fs->len);
  if (r == 0)
    return -1; /* rpicam-vid exited */
  if (r < 0)
    return (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) ? 0 : -1;
  fs->len += (size_t)r;

  for (;;) {
    ssize_t soi = find_soi(fs->buf, fs->len, 0);
    if (soi < 0)
      break;
    if (soi > 0) {
      /* Junk ahead of the first SOI (only possible right after start-up). */
      fs->dropped_bytes += (uint64_t)soi;
      memmove(fs->buf, fs->buf + soi, fs->len - (size_t)soi);
      fs->len -= (size_t)soi;
      continue;
    }
    ssize_t eoi = find_eoi(fs->buf, fs->len, 2);
    if (eoi < 0)
      break; /* frame still arriving */

    fs->frames++;
    emitted++;
    if (cb)
      cb(fs->buf, (size_t)eoi, arg);

    memmove(fs->buf, fs->buf + eoi, fs->len - (size_t)eoi);
    fs->len -= (size_t)eoi;
  }

  if (fs->len > FS_MAX_BUFFERED) {
    fs->dropped_bytes += fs->len;
    fs->len = 0;
  }
  return emitted;
}

void
fs_close(frame_source_t *fs) {
  if (fs->pid > 0) {
    kill(fs->pid, SIGTERM);
    waitpid(fs->pid, NULL, 0);
    fs->pid = 0;
  }
  if (fs->fd >= 0) {
    close(fs->fd);
    fs->fd = -1;
  }
  free(fs->buf);
  fs->buf = NULL;
  fs->len = fs->cap = 0;
}
