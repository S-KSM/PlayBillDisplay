#!/bin/sh
# Runs ON THE KINDLE over SSH, once:  sh /mnt/us/playbills/install_cron.sh
# Adds a cron entry that runs update_playbill.sh every minute. Safe to re-run;
# replaces an older entry for the same script.
LINE='* * * * * sh /mnt/us/playbills/update_playbill.sh'
CRONTAB=/etc/crontab/root

if grep -qxF "$LINE" "$CRONTAB" 2>/dev/null; then
    echo "Cron entry already present."
else
    mntroot rw || { echo "mntroot rw failed"; exit 1; }
    grep -vF "/mnt/us/playbills/update_playbill.sh" "$CRONTAB" > /tmp/crontab.new
    echo "$LINE" >> /tmp/crontab.new
    cat /tmp/crontab.new > "$CRONTAB"
    mntroot ro
    echo "Installed: $LINE"
fi
restart cron 2>/dev/null || /etc/init.d/cron restart 2>/dev/null || { killall crond; crond -c /etc/crontab; }
sh /mnt/us/playbills/update_playbill.sh && echo "Drew first Playbill. Log:" && cat /tmp/playbill.log
