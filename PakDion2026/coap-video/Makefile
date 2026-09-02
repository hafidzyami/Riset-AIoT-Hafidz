# CoAP live video: RFC 7959 vs RFC 9177
#
# Expects libcoap installed under $(COAP_PREFIX) (default ~/.local), built with
# -DENABLE_Q_BLOCK=ON.  See bench/build_libcoap.sh.

COAP_PREFIX ?= $(HOME)/.local

CC      ?= cc
CFLAGS  ?= -O2 -g -std=gnu11 -Wall -Wextra -Wno-unused-parameter
CPPFLAGS += -I$(COAP_PREFIX)/include -Isrc
LDFLAGS  += -L$(COAP_PREFIX)/lib
LDLIBS   += -lcoap-3

BIN     := bin/coap-video-server bin/coap-video-client
SRV_OBJ := build/coap_video_server.o build/frame_source.o
CLI_OBJ := build/coap_video_client.o

.PHONY: all clean
all: $(BIN)

bin/coap-video-server: $(SRV_OBJ) | bin
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

bin/coap-video-client: $(CLI_OBJ) | bin
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

build/%.o: src/%.c | build
	$(CC) $(CFLAGS) $(CPPFLAGS) -c -o $@ $<

bin build:
	mkdir -p $@

clean:
	rm -rf build bin
