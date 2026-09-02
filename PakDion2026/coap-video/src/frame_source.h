/*
 * frame_source.h -- MJPEG frame source backed by rpicam-vid.
 *
 * rpicam-vid is spawned once with "--codec mjpeg -o -" and writes a
 * concatenation of complete JPEG images to a pipe.  fs_pump() drains whatever
 * the kernel has buffered and hands every complete image to a callback, so the
 * whole thing can live inside a single-threaded select() loop next to the CoAP
 * sockets -- no locking, and no chance of a half-written frame being served.
 */
#ifndef FRAME_SOURCE_H_
#define FRAME_SOURCE_H_

#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

typedef struct {
  int      width, height, framerate, quality;
  int      hflip, vflip, rotation;
  unsigned denoise_off;
} fs_config_t;

typedef void (*fs_frame_cb)(const uint8_t *jpeg, size_t len, void *arg);

typedef struct {
  pid_t    pid;
  int      fd;         /* read end of rpicam-vid stdout */
  uint8_t *buf;        /* accumulation buffer           */
  size_t   len, cap;
  uint64_t frames, dropped_bytes;
  fs_config_t cfg;
} frame_source_t;

/* Spawn rpicam-vid.  Returns 0 on success, -1 on failure (errno set). */
int  fs_open(frame_source_t *fs, const fs_config_t *cfg);

/* Read what is available and invoke @p cb once per complete JPEG.
 * Returns the number of frames emitted, or -1 if the pipe closed/errored. */
int  fs_pump(frame_source_t *fs, fs_frame_cb cb, void *arg);

void fs_close(frame_source_t *fs);

#endif /* FRAME_SOURCE_H_ */
