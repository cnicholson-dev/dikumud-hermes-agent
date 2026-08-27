#!/bin/sh
# Copyright (C) 2026 Cody Nicholson
# SPDX-License-Identifier: LGPL-2.1-only
#
# Seeds the persistent data volume on first start, then runs the game server.
#
# DikuMUD chdir()s into its data directory and keeps read-only world files and
# runtime-written state in that same directory (db.h sets DFLT_DIR "lib"):
#
#   read-only : tinyworld.{wld,mob,obj,zon,shp}, help, help_table, credits,
#               news, motd, messages, actions, info, wizlist, poses
#   written   : players, time, ideas, typos, bugs, board.messages
#
# So the volume cannot simply be mounted empty over the world files, and the
# directory cannot be read-only either. The image therefore ships a pristine
# copy at $DIST_DIR and this script populates $DATA_DIR from it exactly once.
# Later starts find it populated and leave player state untouched, which is
# what GAME-04 verifies.

set -eu

DATA_DIR=/var/lib/dikumud/data
DIST_DIR=/opt/dikumud/lib-dist
PORT=4000

# tinyworld.wld is the sentinel: without it the server cannot boot at all, so
# its absence is an unambiguous "this volume has never been seeded".
if [ ! -f "$DATA_DIR/tinyworld.wld" ]; then
    echo "[dikumud] data directory is empty; seeding stock world from image"
    cp -a "$DIST_DIR/." "$DATA_DIR/"
else
    echo "[dikumud] existing data directory found; preserving player state"
fi

echo "[dikumud] starting server on port ${PORT}"
exec /opt/dikumud/bin/dmserver -d "$DATA_DIR" "$PORT"
