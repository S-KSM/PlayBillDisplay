# PlayBill Display

Turn a jailbroken Kindle into a frame for your Playbill collection.

Photograph your Playbills, drop the photos into a small app on your Mac, and the Kindle shows
them full-screen on its e-ink display, a different one each day (or whichever you pick).
The Mac app keeps your collection: show, theatre, the date you went, notes, and the cast list
read straight off the cast page.

- **Drop in photos:** the Playbill is found in the photo, cut out and straightened, so only
  the Playbill shows. Drag four corners yourself when it guesses wrong.
- **Made for e-ink:** each photo is converted to the Kindle's 16 grays, tuned so marker
  signatures stay visible (a blue Sharpie on a gray cover disappears in plain grayscale).
- **Reads the text for you:** show and theatre from the cover, and `Role — Actor` lines
  from a photo of the cast page, using macOS's on-device text recognition.
- **Syncs over Wi-Fi:** pick what's on the Kindle from the Mac. It switches within a minute.
  With the Mac off, the Kindle keeps cycling what it already has.

Not affiliated with Playbill, Inc. "Playbill" is their trademark; this is a fan project for
displaying your own copies.

## What you need

- A Mac (the menu bar app and text recognition are macOS only) with [uv](https://docs.astral.sh/uv/).
- A jailbroken Kindle with SSH access. Built and tested on a Kindle Paperwhite (11th gen,
  firmware 5.19) with [Jobman](https://github.com/KindleTweaks/jobman) for SSH. Other
  jailbroken models should work; set their screen size (see below).

## Install the Mac app

    git clone <this repo> && cd PlayBillDisplay
    ./build_app.sh

This builds **PlayBill Display.app** into `/Applications`. Open it: it lives in the menu bar
as 🎭 (no Dock icon) and opens your library in the browser. The menu has:

- **Open Library…**
- **Kindle checked in … / Showing …**: whether the Kindle is in touch, and what it shows
- **Show Next Playbill on Kindle**, and **Unpin** while one is kept up
- **Start at Login**: keeps the sync running whenever you're logged in
- **Quit**

The app runs the code in this folder, so after moving the folder, run `./build_app.sh` again.
The log is at `~/Library/Logs/PlayBillDisplay.log`.

You can also run it without the app bundle: `uv run app.py`, then open http://localhost:8780.

## Set up the Kindle (once)

1. Plug the Kindle into the Mac by USB. **Install** appears at the top of the library; click
   it. It copies the scripts and your Playbills to `/mnt/us/playbills` on the Kindle, and
   writes this Mac's network address into `config.sh` there. Then click **Eject**.
2. On the Kindle, turn on SSH (in Jobman: Wi-Fi SSH) and note its IP address.
3. From the Mac, schedule the display script:

       ssh root@<kindle-ip> 'sh /mnt/us/playbills/install_cron.sh'

   This adds a once-a-minute entry to the Kindle's cron, which survives reboots, and draws
   the first Playbill. It is safe to run again.
4. Turn SSH off again, and keep the Kindle on Wi-Fi and plugged into power.

From then on, everything syncs over Wi-Fi while the Mac app is running.

## Using it

- **Add Playbills:** drop photos (JPG, PNG, HEIC) anywhere on the library page, or click the
  last spot on the wall. The newest one goes up on the Kindle right away.
- **Now showing:** the top of the page shows exactly what's on the Kindle. **Show the next
  one** moves along; **Keep this one up** stops the daily rotation until you resume it.
- **Click a Playbill** to open it:
  - **Details:** show, theatre, date seen, notes. Saves as you type.
  - **Cast:** add a photo of the cast page. The names are read on your Mac; check them, then
    add them to the cast list. Search covers the cast too.
  - **Picture:** crop (Automatic, Corners, Whole photo) and the look for e-ink (Signed,
    Natural, Bold, or fine-tune with sliders), with a live preview. **Send to Kindle** applies it.
  - **Show on Kindle**, **Keep it up**, reorder with **Earlier/Later**, or **Delete**.

## How the Kindle side works

Cron runs `kindle/playbills/update_playbill.sh` every minute. Each run:

- downloads new Playbills from the Mac and, once everything has arrived, removes deleted ones;
- applies a "show this one" or "keep it up" request from the app;
- moves to the next Playbill once `ROTATE_MINUTES` have passed (in `config.sh`; default
  1440, one a day; 0 stays put);
- redraws on change, and quietly every 5 minutes so the image comes back if a tap woke the
  Kindle's own screens;
- reports back what it's showing.

It does nothing while the Kindle is mounted on a computer as a USB drive. To skip ahead by
hand: `sh /mnt/us/playbills/update_playbill.sh next`. Its log is `/tmp/playbill.log`.

## Other Kindle models

The default screen size is the Paperwhite 11th gen's 1236×1648. For others, set `W` and `H`
before starting, e.g. `W=1072 H=1448 uv run app.py` (Paperwhite 3/4) or `W=600 H=800`
(basic Kindle). Other settings: `PORT` (default 8780), `MARGIN`, `KINDLE` (USB mount
point, default `/Volumes/Kindle`).

## Things to know

- **Your network:** the app listens on your local network so the Kindle can reach it, and has
  no login. Anyone on the same Wi-Fi can open the library and change it. Run it on a network
  you trust.
- **SSH:** Jobman's SSH ships with a default password. Leave SSH off when you're not using
  it, or change the password.
- **The Mac's address:** if the router gives your Mac a new IP, the Kindle loses it. Click
  Install again, or give the Mac a fixed address in your router.
- **Power:** the script keeps the Kindle's screensaver off so the Playbill stays up, which
  keeps the Kindle awake. Keep it plugged in.
- **Your data** stays on your Mac in `library/` (photos, notes, converted images). It is not
  part of the repo.

## Code

| File | What it does |
| --- | --- |
| `app.py` | Web server: library, image and text endpoints, Kindle sync endpoints |
| `imaging.py` | Finding and straightening the Playbill in a photo; e-ink conversion |
| `ocr.py` | Cover and cast-page text recognition (macOS Vision) |
| `menubar.py` | The menu bar app that runs the server |
| `build_app.sh` | Builds `PlayBill Display.app` |
| `static/index.html` | The library page |
| `kindle/playbills/` | Scripts that run on the Kindle |
