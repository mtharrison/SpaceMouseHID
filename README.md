# SpaceMouseHID

A Fusion add-in for macOS that drives the viewport from a 3Dconnexion SpaceMouse **without the 3Dconnexion driver**.

Useful when the official driver can't load, for example when a company's device management (MDM) blocks its system extension (`OSSystemExtensionErrorForbiddenBySystemPolicy`).

It reads the device directly over USB HID using macOS's IOKit through Python `ctypes`. There are no dependencies, no extension and no admin rights.

## Install

```bash
git clone https://github.com/mtharrison/SpaceMouseHID.git \
  "$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/SpaceMouseHID"
```

In Fusion, press Shift+S, open **Add-Ins**, select **SpaceMouseHID** and click **Run**. It is set to run on startup.

## Use

- Push, pull, lift, tilt and twist the cap to move the view.
- Left button: fit to view. Right button: home view.
- **Utilities → Add-Ins → SpaceMouse Settings** opens these options:
  - **Speed**: 0.25× to 4×.
  - **Invert**: a tickbox for each of the six axes.
  - **Selected part**: what to do with a selected or activated body or component. Orbit around it with free panning, keep it centred, or ignore it.

  Changes apply live while the dialog is open. **Save** keeps them in `settings.json`; **Cancel** reverts.

## Notes

- The add-in opens the SpaceMouse exclusively while it runs. Otherwise macOS's generic HID driver also turns the cap's motion into scroll events, and Fusion zooms on those.
- Tested with a SpaceMouse Compact (USB). Other 3Dconnexion devices (vendor `0x256F`) that report the standard six axes should work. Scale the input range with `FULL_SCALE` in `hid_mac.py`.
- If the official driver is installed and working, stop this add-in so the two don't both move the camera.
- Problems are logged to `spacemouse.log` in the add-in folder. Set `DEBUG = True` in `SpaceMouseHID.py` for per-frame camera logging.
