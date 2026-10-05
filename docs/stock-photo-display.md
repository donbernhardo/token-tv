# Build note: drawing on a desk clock through its photo album

![TokenTV on a desk clock](images/real-clock.jpg)

![Six clock faces](images/clock-faces.png)

TokenTV shows Claude and Codex usage limits on a small 240×240 Wi-Fi desk clock. It does
not replace the clock's firmware. This note explains how, and where it stops working.

## The idea

The clock used for this project ships with a web page for uploading pictures to a photo
album that rotates on the screen. If the clock can show any picture, the computer can draw the
numbers itself and send the result as a picture. The clock only displays the image. It never
runs TokenTV code, so there is nothing to flash and no firmware to restore.

## The flow

1. **Read usage.** A computer on the same network reads usage through each tool's own
   login: the Claude Code CLI's OAuth profile and usage, the official `codex app-server`
   rate-limit RPC, and, as an experimental option, the Grok CLI billing RPC (a share of the CLI
   billing budget, not web chat limits). It polls every five minutes by default.
2. **Draw a face.** Python and Pillow draw a 240×240 image: used percentage, window,
   reset time and account label. An old reading is marked OLD, and missing data shows a dash
   instead of a fake 0%.
3. **Upload a picture.** The image goes to `/photo/upload` (SD_PRO) or `/doUpload?dir=/image/` (stock GeekMagic) as a normal
   file upload: `tokentv.jpg` (kept under 60 KB), or `tokentv.gif` for the animated face.
4. **Show only that picture.** TokenTV records readable original settings, verifies its uploaded
   file is present, deletes every other album image, and switches to the photo theme.
   Stock SmallTV-PRO uses Picture theme `4` and a one-image slideshow. PRO uploads are also
   verified by downloading and comparing their bytes.
5. **Restore settings.** A restore command puts readable saved theme/settings back.
   Existing photos are not backed up; deleted photos cannot be restored.

The clock receives only a picture, never a password, token or cookie. An identical
image is not uploaded again; a new one is sent only when the rendered picture differs from the last one.

## Why a picture

A picture is the one thing the stock firmware already accepts and keeps showing. Drawing
on the computer also means a clock face is just a Python function. The
[four-colour Game Boy example](../examples/gameboy) changes the look without touching the clock:

![Pixel face and Game Boy face](../examples/gameboy/comparison.png)

It runs with built-in sample readings, so no clock or login is needed to try a new face.

## Limits

- Verified through HTTP on SD_PRO and SmallTV-PRO V3.3.75EN units, driven from a Linux host.
  Other firmware, or different photo API paths,
  may not work. See [compatible clocks](hardware-compatibility.md).
- It needs an always-on computer on the same network. If that computer sleeps, the clock
  simply keeps showing the last picture it received.

TokenTV was built with AI assistance (Claude Code). The author reviewed and tested it on the hardware above.

Demo https://token-tv.vercel.app · GitHub https://github.com/click6067-ship-it/token-tv
