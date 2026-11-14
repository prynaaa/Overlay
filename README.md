# ASCII Overlay

A single-file, dependency-free Python desktop overlay that renders animated
ASCII art on a transparent, always-on-top canvas. Built with tkinter for
Windows, with notes for macOS and Linux below.

```
python overlay.py
```

No installation and no third-party packages are required (Python 3.9+ with
tkinter, which ships with the official Windows/macOS installers and is usually
available as `python3-tk` on Linux).

## Controls

| Action | How |
| --- | --- |
| Move the overlay | Drag with the left mouse button |
| Open settings | Right-click the overlay, press `Ctrl+Alt+O`, or right-click / double-click the tray icon |
| Close settings | `Esc` or the window close button (the overlay keeps running) |
| Save settings | Automatic, plus **Save now** / `Ctrl+S` in settings |
| Quit | **Quit** in the settings window |

When **Click-through** is enabled, mouse input goes to the desktop beneath the
overlay. Use `Ctrl+Alt+O` or the tray icon to reopen settings and turn it off.
If the tray icon and hotkey are both unavailable, the app automatically starts
with click-through disabled so the window cannot become unreachable.

## Animations

1. **Butterflies** – three-frame flapping sprites drifting along sine paths.
2. **Koi Pond** – koi steered through a smooth value-noise flow field, with
   fading bodies, fins and expanding ripples.
3. **Rain** – windless streaks that spawn gravity-driven splash particles.
4. **Fireflies** – slow noise-steered wanderers with soft blinking halos.
5. **Snow** – flakes of varying size, depth and sway.
6. **Ocean Waves** – layered sine crests with foam, glints and a sparse water
   texture.
7. **Starfield & Clouds** – parallax twinkling stars behind drifting, textured
   noise clouds.

Every animation derives from `Animation` and implements:

```python
update(dt)     # advance simulation; dt is already scaled by the speed slider
render(grid)   # write brightness values in the range 0.0 - 1.0
```

Brightness is mapped through `CHAR_RAMP = " .:-=+*#%@"` and drawn as a
monospace character grid at roughly 30 FPS using tkinter's `after()`.

## Settings

The settings window provides:

- animation dropdown
- opacity slider (0.1 – 1.0, applied to the whole window)
- animation speed slider (0.1 – 3.0)
- font-size / grid-density slider (6 – 32 px)
- colour picker for the text
- click-through and always-on-top checkboxes
- X/Y position and width/height fields, plus **Apply** and **Center**
- **Save now**, **Hide** and **Quit** buttons

Settings are stored as JSON next to `overlay.py` as `overlay_settings.json`.
If that directory is read-only, the fallback location is
`%APPDATA%\AsciiOverlay\overlay_settings.json` on Windows (similarly under the
home directory on other platforms). They are loaded automatically on start and
saved with a short debounce as values change.

Example:

```json
{
  "animation": "Koi Pond",
  "opacity": 0.9,
  "speed": 1.0,
  "font_size": 14,
  "color": "#A6F2C1",
  "click_through": false,
  "always_on_top": true,
  "x": 370,
  "y": 250,
  "width": 960,
  "height": 540
}
```

## How transparency works

On Windows the overlay uses:

- `wm_attributes("-transparentcolor", "#010203")` for a chroma-key background,
  so only the glyphs appear over the desktop;
- `wm_attributes("-alpha", ...)` for the opacity slider;
- `overrideredirect(True)` plus `WS_EX_TOOLWINDOW` and removal of
  `WS_EX_APPWINDOW` to keep the overlay out of the taskbar/Alt+Tab;
- `WS_EX_TRANSPARENT` toggled at runtime for click-through;
- `WS_EX_NOACTIVATE` so the overlay never steals focus;
- a hidden Win32 message window (via `ctypes`) for the tray icon and the
  global `Ctrl+Alt+O` hotkey;
- a 30 FPS tkinter `after()` loop drawing one canvas text item per character
  row (far cheaper than thousands of per-character canvas items).

The chroma-key background is `#010203`, which keeps anti-aliasing fringes
subtle. Plain tkinter cannot produce true per-pixel alpha on Windows, so very
light wallpapers may show a faint dark edge around glyphs; use a larger font or
a darker wallpaper if that is distracting, or replace the canvas renderer with
a per-pixel-alpha image renderer.

## Platform notes

### Windows (primary target)

Everything described above is implemented. `python overlay.py` works as-is.
Running it via `pythonw.exe overlay.py` avoids a console window.

### macOS

`-transparentcolor` is Windows-only. The app attempts Tk 8.6's
`wm_attributes("-transparent", True)` with a `systemTransparent` background,
which gives varying results depending on the Tk build and compositor.
Click-through, tray and global hotkey are not implemented for macOS. For
reliable per-pixel transparency on macOS, use a small PyObjC/native helper
window or render into an RGBA image and place it with a native layer; plain
tkinter cannot guarantee it. Right-clicking the overlay (keep click-through
off) still opens settings, and `-alpha` opacity works.

### Linux / X11

Tk has no portable chroma-key window. The window will show the
`#010203` background as a solid dark rectangle; `-alpha` opacity only works with
a compositing window manager. True transparency requires an ARGB visual or an
XShape mask supplied by a toolkit/extension outside plain tkinter. Tray and
global hotkey code is Windows-only; right-click the overlay instead. Drag,
settings, animation and persistence work normally.

## Customising

- Add a new animation by subclassing `Animation`, implementing
  `update`/`render`, and adding the class to the `ANIMATIONS` tuple near the
  bottom of the animation section.
- Change `CHAR_RAMP` to use a different brightness ramp.
- Change `FPS`, `TRANSPARENT_KEY`, or `Settings.for_screen()` for different
  defaults.

## Files

- `overlay.py` – the complete application (standard library only)
- `overlay_settings.json` – generated at runtime (not committed)
