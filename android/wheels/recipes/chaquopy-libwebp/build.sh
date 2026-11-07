#!/bin/bash
set -eu

# Pillow's _webp extension links THREE libraries (webp, webpmux, webpdemux) —
# see setup.py's `libs = [webp, webp + "mux", webp + "demux"]`. configure
# defaults mux and demux off, so both are enabled explicitly; without them the
# link fails rather than silently producing a half-featured module.
#
# The png/jpeg/tiff/gif/wic switches are about the cwebp/dwebp COMMAND-LINE
# tools, not the library. Left on, configure probes the BUILD machine's
# /usr/include and links Android executables against host libraries.
./configure --host=$HOST --prefix=$PREFIX --disable-static \
    --enable-libwebpmux --enable-libwebpdemux \
    --disable-png --disable-jpeg --disable-tiff --disable-gif --disable-wic
make -j $CPU_COUNT
make install

# build-wheel packages $PREFIX wholesale, and the tools are cross-compiled
# Android executables nothing on the device runs. Same reason
# chaquopy-libjpeg's build.sh does `rm -r $PREFIX/{bin,doc,man}`.
rm -rf $PREFIX/bin $PREFIX/share
rm -f $PREFIX/lib/*.la
