# Widevine Device Setup

Kagane’s DRM-protected CDN only serves images to clients that can
produce a valid Widevine L3 license challenge. The downloader relies on
[`pywidevine`](https://github.com/medvm/pywidevine) to emulate the same
flow, which means you must provide your own Widevine device
certificate (`*.wvd`) sourced from hardware or an emulator you control.

> ⚠️ **Important:** Only extract keys from devices you own and ensure
> this process is legal in your jurisdiction. We cannot ship Widevine
> blobs with the project for both legal and ethical reasons.

## 1. Install prerequisites

```bash
python3 -m pip install pywidevine
```

If you are using a virtual environment, make sure it is active before
installing.

## 2. Provision an L3 device

Follow the pywidevine documentation for provisioning:

1. Review [pywidevine’s provisioning guide](https://github.com/medvm/pywidevine#device-provisioning).
2. Use an Android device (or emulator) that still supports Widevine
   L3. The linked guide explains how to leverage Frida or ADB to export
   the device keys.
3. Save the resulting `.wvd` file to your workstation. This file
   contains the device certificate, private key, and provisioning
   metadata required to talk to Widevine license servers.

## 3. Place the `.wvd` file where the downloader can find it

The Kagane handler searches for device blobs in this order:

1. The path specified via the `KAGANE_WVD` environment variable.
2. Any file ending with `.wvd` in the repository root directory.

Example:

```bash
export KAGANE_WVD=/path/to/my_device.wvd
python3 comick_downloader.py --site kagane --comic-url https://kagane.org/series/...
```

If you prefer not to set the environment variable, simply copy the
`.wvd` file into the project root (next to `comick_downloader.py`).

## 4. Keep your device file safe

Treat the `.wvd` file like credentials:

- Do not commit it to source control.
- Do not share it.
- Remove it if you no longer need Kagane downloads.

Once the file is in place, rerun the downloader. The Kagane handler will
use your device certificate to generate the required Widevine challenge,
fetch access tokens, and decrypt the DRM-protected images.
