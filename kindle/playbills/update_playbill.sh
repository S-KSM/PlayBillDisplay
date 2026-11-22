#!/bin/sh
# Runs ON THE KINDLE from cron every minute (see install_cron.sh). Each run:
#   1. pulls new/removed Playbills from the Mac (if SERVER is set and reachable),
#   2. applies a "show this one" / pin request made in the Mac app,
#   3. otherwise advances to the next Playbill once ROTATE_MINUTES have passed,
#   4. redraws the screen on change, and quietly every 5 minutes (so the image comes
#      back after the Kindle UI paints over it), then tells the Mac what is showing.
# Optional argument "next" forces the next Playbill now.

export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"   # cron gives a minimal PATH; eips is in /usr/sbin
DIR=${PLAYBILL_DIR:-/mnt/us/playbills}
STATE=$DIR/.state
LOCK=/tmp/playbill.lock
LOG=/tmp/playbill.log

SERVER=""
ROTATE_MINUTES=1440
SHOW_NEW_IMMEDIATELY=1
# While the Kindle is plugged into a computer as a USB drive, /mnt/us is unmounted and
# this folder looks empty. Do nothing then, and leave whatever is on screen alone.
[ -f "$DIR/update_playbill.sh" ] || exit 0
[ -f "$DIR/config.sh" ] && . "$DIR/config.sh"

mkdir "$LOCK" 2>/dev/null || exit 0          # a previous run is still going
trap 'rmdir "$LOCK"' EXIT INT TERM
mkdir -p "$STATE"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# Keep the screensaver from replacing the image (keep the Kindle plugged in).
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null

now=$(date +%s)
cur=$(cat "$STATE/current" 2>/dev/null)
since=$(cat "$STATE/since" 2>/dev/null)
case "$since" in ''|*[!0-9]*) since=0 ;; esac
newest=""
online=0
forced=""
pinned=$(cat "$STATE/pin" 2>/dev/null)
[ "$pinned" = 1 ] || pinned=0

# ---- 1. sync from the Mac ----
if [ -n "$SERVER" ] && wget -q -T 10 -O /tmp/pb_list.txt "$SERVER/kindle/list.txt" 2>/dev/null; then
    complete=1
    while read -r f; do
        case "$f" in ''|*/*|.*) continue ;; esac
        [ -f "$DIR/$f" ] && continue
        if wget -q -T 60 -O "$DIR/$f.part" "$SERVER/kindle/img/$f" 2>/dev/null; then
            mv "$DIR/$f.part" "$DIR/$f"
            newest=$f
            log "downloaded $f"
        else
            rm -f "$DIR/$f.part"
            complete=0
            log "failed to download $f"
        fi
    done < /tmp/pb_list.txt
    # Only clean up once everything new has arrived, so a failed or partial sync
    # (Mac asleep, app restarting) can never leave the Kindle with nothing to show.
    if [ "$complete" = 1 ]; then
        for p in "$DIR"/*.png; do
            [ -e "$p" ] || continue
            b=$(basename "$p")
            case "$b" in ._*) rm -f "$p"; continue ;; esac
            grep -qxF "$b" /tmp/pb_list.txt || { rm -f "$p"; log "removed $b"; }
        done
        cp /tmp/pb_list.txt "$DIR/order.txt"
    fi
    online=1
fi

# ---- 1b. "show this one" request from the Mac app: "<seq> <pin> <file|->" ----
if [ "$online" = 1 ] && wget -q -T 10 -O /tmp/pb_cmd.txt "$SERVER/kindle/command.txt" 2>/dev/null; then
    read -r cseq cpin cname < /tmp/pb_cmd.txt
    lastseq=$(cat "$STATE/seq" 2>/dev/null)
    case "$cseq" in ''|*[!0-9]*) cseq="" ;; esac
    case "$cname" in */*|.*) cname="" ;; esac
    if [ -n "$cseq" ] && [ "$cseq" != "$lastseq" ]; then
        if [ "$cname" = - ] || [ -f "$DIR/$cname" ]; then   # else retry next run
            pinned=$([ "$cpin" = 1 ] && echo 1 || echo 0)
            echo "$pinned" > "$STATE/pin"
            echo "$cseq" > "$STATE/seq"
            [ "$cname" != - ] && forced=$cname
            log "request $cseq: show=$cname pin=$pinned"
        fi
    elif [ "$pinned" = 1 ] && [ -n "$cname" ] && [ "$cname" != - ] && [ -f "$DIR/$cname" ]; then
        forced=$cname   # pinned image was re-rendered (e.g. rotated) under a new name
    fi
fi

# ---- 2. build the playlist: order.txt first, then any other PNGs ----
: > /tmp/pb_play.txt
[ -f "$DIR/order.txt" ] && while read -r f; do
    [ -n "$f" ] && [ -f "$DIR/$f" ] && echo "$f" >> /tmp/pb_play.txt
done < "$DIR/order.txt"
for p in "$DIR"/*.png; do
    [ -e "$p" ] || continue
    b=$(basename "$p")
    case "$b" in ._*) continue ;; esac   # macOS metadata files, not images
    grep -qxF "$b" /tmp/pb_play.txt || echo "$b" >> /tmp/pb_play.txt
done

if [ ! -s /tmp/pb_play.txt ]; then
    eips -c; eips 2 2 "No Playbills in $DIR yet"
    exit 0
fi

next_after() {  # print the entry after $1 in the playlist (wraps around)
    awk -v c="$1" 'NR==1{first=$0} hit{print; found=1; exit} $0==c{hit=1} END{if(!found) print first}' /tmp/pb_play.txt
}

# ---- 3. pick what to show ----
show=$cur
if [ -n "$forced" ]; then
    show=$forced
elif [ -n "$newest" ] && [ -n "$cur" ] && [ "$pinned" = 0 ] && [ "$SHOW_NEW_IMMEDIATELY" = 1 ]; then
    show=$newest
elif ! grep -qxF "$cur" /tmp/pb_play.txt 2>/dev/null || [ -z "$cur" ]; then
    show=$(head -n 1 /tmp/pb_play.txt)
elif [ "$1" = next ]; then
    show=$(next_after "$cur")
elif [ "$pinned" = 0 ] && [ "$ROTATE_MINUTES" -gt 0 ] 2>/dev/null && [ $(( now - since )) -ge $(( ROTATE_MINUTES * 60 )) ]; then
    show=$(next_after "$cur")
fi

# ---- 4. draw ----
if [ "$show" != "$cur" ]; then
    echo "$show" > "$STATE/current"
    echo "$now" > "$STATE/since"
    log "showing $show"
    eips -c
    eips -f -g "$DIR/$show"        # full refresh: clears ghosting from the old image
elif [ $(( (now / 60) % 5 )) -eq 0 ]; then
    eips -g "$DIR/$show"           # quiet redraw in case the Kindle UI drew over it
fi

[ "$online" = 1 ] && wget -q -T 5 -O /dev/null "$SERVER/kindle/showing?name=$show" 2>/dev/null
