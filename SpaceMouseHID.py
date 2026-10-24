"""Drive Fusion's viewport from a SpaceMouse without the 3Dconnexion driver.

Camera mode: the view moves the way you push the cap. Left button = fit, right button = home.
A selected body/component (or the activated one) becomes the orbit point.
Speed, inversion and selection mode live in Utilities > Add-Ins > SpaceMouse Settings.
"""
import adsk.core
import adsk.fusion
import importlib
import json
import math
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(__file__))
import hid_mac  # noqa: E402

# Device axes are X right, Y toward you, Z down (raw HID), in order tx ty tz rx ry rz.
AXIS_LABELS = ['Push left / right (pan)', 'Pull / push (zoom)', 'Lift / press (pan up / down)',
               'Tilt forward / back', 'Tilt left / right', 'Twist']
SETTINGS = os.path.join(os.path.dirname(__file__), 'settings.json')
CMD_ID = 'SpaceMouseHID_settings'
PANEL_ID = 'SolidScriptsAddinsPanel'  # Utilities > Add-Ins
DEADZONE = 0.05                   # fraction of full deflection
ROTATE_SPEED = 2.0                # rad/s at full deflection
PAN_SPEED = 1.0                   # view-sizes/s at full deflection
ZOOM_SPEED = 1.5                  # e-folds/s at full deflection
DEFAULT_SPEED = 1.5               # multiplier on the three above; Speed slider in settings
RECENTRE_RATE = 8.0               # 1/s; how quickly a selection glides to the centre
SELECTION_MODES = {               # prefs['selection'] -> label in settings
    'orbit': 'Orbit around it, pan freely',
    'centre': 'Keep it centred, no panning',
    'off': 'Ignore selection',
}
RATE_HZ = 60
RESYNC_AFTER = 0.5                # s idle before picking up mouse-driven view changes
DEBUG = False
EVENT_ID = 'SpaceMouseHID_tick'
LOG = os.path.join(os.path.dirname(__file__), 'spacemouse.log')

app = None
mouse = None
ticker = None
handlers = []
state = {'running': False, 'pending': False, 'last': 0.0, 'presses': [], 'ticks': 0,
         'cam': None, 'cam_time': 0.0}
prefs = {'invert': [False] * 6, 'speed': DEFAULT_SPEED, 'selection': 'orbit'}
dialog = {'saved': None, 'done': False}


def log(msg):
    with open(LOG, 'a') as f:
        f.write(time.strftime('%H:%M:%S ') + msg + '\n')


def load_settings():
    try:
        with open(SETTINGS) as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    inv = saved.get('invert', [])[:6]
    prefs['invert'] = [bool(v) for v in inv] + [False] * (6 - len(inv))
    prefs['speed'] = float(saved.get('speed', DEFAULT_SPEED))
    mode = saved.get('selection', 'orbit')
    prefs['selection'] = mode if mode in SELECTION_MODES else 'orbit'


def save_settings():
    with open(SETTINGS, 'w') as f:
        json.dump(prefs, f, indent=2)


def read_dialog(inputs):
    prefs['invert'] = [inputs.itemById('invert%d' % i).value for i in range(6)]
    prefs['speed'] = inputs.itemById('speed').valueOne
    label = inputs.itemById('selection').selectedItem.name
    prefs['selection'] = next(k for k, v in SELECTION_MODES.items() if v == label)


def selection_centre():
    """Centre of the selected entities' bounds, else of the activated component, else None."""
    if prefs['selection'] == 'off':
        return None
    boxes = []
    sels = app.userInterface.activeSelections
    for i in range(sels.count):
        try:
            boxes.append(sels.item(i).entity.boundingBox)
        except Exception:
            pass  # entity types without bounds (e.g. some sketch items)
    if not boxes:
        design = adsk.fusion.Design.cast(app.activeProduct)
        occ = design and design.activeOccurrence
        if occ:
            boxes.append(occ.boundingBox)
    if not boxes:
        return None
    lo = [min(getattr(b.minPoint, k) for b in boxes) for k in 'xyz']
    hi = [max(getattr(b.maxPoint, k) for b in boxes) for k in 'xyz']
    return adsk.core.Point3D.create(*((a + b) / 2 for a, b in zip(lo, hi)))


def shaped(v):
    a = abs(v)
    if a < DEADZONE:
        return 0.0
    return math.copysign(min(1.0, (a - DEADZONE) / (1 - DEADZONE)), v)


def move_camera(axes, dt):
    vp = app.activeViewport
    cam = vp.camera
    ortho = cam.cameraType == adsk.core.CameraTypes.OrthographicCameraType
    # Fusion adjusts the camera we set; reading that back every frame compounds
    # into runaway drift, so keep our own copy while the cap is in use.
    now = time.perf_counter()
    held = state['cam']
    if held and now - state['cam_time'] < RESYNC_AFTER:
        eye, target, up, extents = held[0].copy(), held[1].copy(), held[2].copy(), held[3]
        if DEBUG:
            d0, d1 = eye.distanceTo(target), cam.eye.distanceTo(cam.target)
            if abs(d1 - d0) > 0.01 * d0 or abs(cam.viewExtents - extents) > 0.01 * extents \
                    or cam.target.distanceTo(target) > 0.01 * d0:
                log('fusion changed: dist %.2f -> %.2f ext %.2f -> %.2f target moved %.2f eye moved %.2f' % (
                    d0, d1, extents, cam.viewExtents, cam.target.distanceTo(target), cam.eye.distanceTo(eye)))
    else:
        eye, target, up, extents = cam.eye, cam.target, cam.upVector, cam.viewExtents
    back = target.vectorTo(eye)
    dist = back.length
    back.normalize()
    up.normalize()
    right = up.crossProduct(back)
    right.normalize()
    up = back.crossProduct(right)

    # The maths below is object mode, but camera mode (every axis negated) felt right by default.
    tx, ty, tz, rx, ry, rz = (shaped(a) if inv else -shaped(a) for a, inv in zip(axes, prefs['invert']))
    pivot = selection_centre()
    if pivot and prefs['selection'] == 'centre':
        # Glide the selection to the centre; panning would push it off.
        tx = tz = 0.0
        shift = target.vectorTo(pivot)
        shift.scaleBy(1 - math.exp(-RECENTRE_RATE * dt))
        eye.translateBy(shift)
        target.translateBy(shift)
    dt *= prefs['speed']
    size = extents if ortho else dist

    # Pan: model follows the cap, so the camera moves the other way. Device Z is down.
    pan = adsk.core.Vector3D.create(0, 0, 0)
    for axis, amount in ((right, -tx), (up, tz)):
        v = axis.copy()
        v.scaleBy(amount * PAN_SPEED * size * dt)
        pan.add(v)
    eye.translateBy(pan)
    target.translateBy(pan)

    # Zoom: pulling the cap toward you brings the model closer.
    f = math.exp(-ty * ZOOM_SPEED * dt)
    if ortho:
        extents *= f
    offset = back.copy()
    offset.scaleBy(dist * f)
    eye = target.copy()
    eye.translateBy(offset)

    # Orbit: device frame (X right, Y toward you, Z down) maps to (right, back, -up);
    # turning the model by w turns the camera about the pivot by -w. Rotating about a
    # selection keeps it fixed on screen wherever it has been panned to.
    w = adsk.core.Vector3D.create(0, 0, 0)
    for axis, amount in ((right, rx), (back, ry), (up, -rz)):
        v = axis.copy()
        v.scaleBy(amount)
        w.add(v)
    angle = w.length * ROTATE_SPEED * dt
    if angle > 1e-6:
        w.normalize()  # setToRotation scales by a non-unit axis
        m = adsk.core.Matrix3D.create()
        m.setToRotation(-angle, w, pivot or target)
        eye.transformBy(m)
        target.transformBy(m)
        up.transformBy(m)
        # Re-pin the orbit radius so rounding can't accumulate into drift.
        offset = target.vectorTo(eye)
        offset.normalize()
        offset.scaleBy(dist * f)
        eye = target.copy()
        eye.translateBy(offset)

    cam.isSmoothTransition = False  # before the moves, so none of them animate
    cam.eye, cam.target, cam.upVector = eye, target, up
    if ortho:
        cam.viewExtents = extents
    vp.camera = cam
    vp.refresh()
    state['cam'] = (eye.copy(), target.copy(), up.copy(), extents)
    state['cam_time'] = now

    if DEBUG and state['ticks'] % 10 == 0:
        rb = vp.camera
        log('axes %s | set target (%.1f %.1f %.1f) dist %.1f ext %.1f | read target (%.1f %.1f %.1f) dist %.1f ext %.1f' % (
            ' '.join('%+.2f' % a for a in (tx, ty, tz, rx, ry, rz)),
            target.x, target.y, target.z, eye.distanceTo(target), extents,
            rb.target.x, rb.target.y, rb.target.z, rb.eye.distanceTo(rb.target), rb.viewExtents))


class TickHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            now = time.perf_counter()
            dt = min(now - state['last'], 0.1)
            state['last'] = now
            while state['presses']:
                button = state['presses'].pop(0)
                vp = app.activeViewport
                if button == 1:
                    vp.fit()
                elif button == 2:
                    vp.goHome(False)
            if any(shaped(a) for a in mouse.axes):
                move_camera(list(mouse.axes), dt)
        except Exception:
            log(traceback.format_exc())
        finally:
            state['pending'] = False


class SettingsCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.okButtonText = 'Save'
            inputs = cmd.commandInputs
            slider = inputs.addFloatSliderCommandInput('speed', 'Speed', '', 0.25, 4.0, False)
            slider.valueOne = prefs['speed']
            modes = inputs.addDropDownCommandInput(
                'selection', 'Selected part', adsk.core.DropDownStyles.TextListDropDownStyle)
            for key, label in SELECTION_MODES.items():
                modes.listItems.add(label, key == prefs['selection'])
            for i, label in enumerate(AXIS_LABELS):
                inputs.addBoolValueInput('invert%d' % i, 'Invert ' + label, True, '', prefs['invert'][i])
            # Changes apply live so you can try them with the cap; Cancel restores these.
            dialog['saved'] = dict(prefs, invert=list(prefs['invert']))
            dialog['done'] = False
            for event, handler in ((cmd.inputChanged, SettingsChangedHandler()),
                                   (cmd.execute, SettingsExecuteHandler()),
                                   (cmd.destroy, SettingsDestroyHandler())):
                event.add(handler)
                handlers.append(handler)
        except Exception:
            log(traceback.format_exc())


class SettingsChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            read_dialog(args.inputs)
        except Exception:
            log(traceback.format_exc())


class SettingsExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            read_dialog(args.command.commandInputs)
            save_settings()
            dialog['done'] = True
            log('saved: speed %.2f, selection %s, inverted %s' % (
                prefs['speed'], prefs['selection'], [l for l, v in zip(AXIS_LABELS, prefs['invert']) if v]))
        except Exception:
            log(traceback.format_exc())


class SettingsDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not dialog['done']:
            prefs.update(dialog['saved'])


def add_settings_button():
    ui = app.userInterface
    cmd_def = ui.commandDefinitions.itemById(CMD_ID) or ui.commandDefinitions.addButtonDefinition(
        CMD_ID, 'SpaceMouse Settings', 'Invert SpaceMouse axes')
    on_created = SettingsCreatedHandler()
    cmd_def.commandCreated.add(on_created)
    handlers.append(on_created)
    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    if panel and not panel.controls.itemById(CMD_ID):
        panel.controls.addCommand(cmd_def)


def remove_settings_button():
    ui = app.userInterface
    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    ctrl = panel and panel.controls.itemById(CMD_ID)
    if ctrl:
        ctrl.deleteMe()
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()


def tick_loop():
    while state['running']:
        active = state['presses'] or any(shaped(a) for a in mouse.axes)
        if active and not state['pending']:
            if state['ticks'] == 0:
                log('first motion seen: %s' % ['%+.2f' % a for a in mouse.axes])
            state['ticks'] += 1
            state['pending'] = True
            app.fireCustomEvent(EVENT_ID, '')
        elif not active:
            state['last'] = time.perf_counter()
        time.sleep(1 / RATE_HZ)


def run(context):
    global app, mouse, ticker
    try:
        app = adsk.core.Application.get()
        importlib.reload(hid_mac)  # Fusion keeps modules loaded across Stop/Run
        load_settings()
        add_settings_button()
        event = app.registerCustomEvent(EVENT_ID)
        handler = TickHandler()
        event.add(handler)
        handlers.append(handler)
        mouse = hid_mac.SpaceMouse(on_button=state['presses'].append)
        if not mouse.start():
            log('start failed: %s' % mouse.error)
            return
        state['running'] = True
        state['last'] = time.perf_counter()
        ticker = threading.Thread(target=tick_loop, daemon=True)
        ticker.start()
        log('running (%s access)' % ('exclusive' if mouse.seized else 'shared: may also scroll'))
    except Exception:
        log(traceback.format_exc())


def stop(context):
    try:
        state['running'] = False
        if ticker:
            ticker.join(1)
        if mouse:
            mouse.stop()
        app.unregisterCustomEvent(EVENT_ID)
        remove_settings_button()
        handlers.clear()
        log('stopped after %d ticks' % state['ticks'])
    except Exception:
        log(traceback.format_exc())
