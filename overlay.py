#!/usr/bin/env python3
"""
ASCII Overlay - a transparent, always-on-top ASCII art animation overlay.

Features
--------
* Borderless, always-on-top window with no taskbar entry (Windows).
* Transparent background using Tk's ``-transparentcolor`` window attribute.
* Optional click-through mode, draggable window, 7 animations at ~30 FPS.
* Settings window with animation picker, opacity/speed/font sliders, colour
  picker, window geometry, click-through / always-on-top toggles and JSON
  persistence.
* Optional system-tray icon and global hotkey (Ctrl+Alt+O) on Windows.

Usage
-----
    python overlay.py

Right-click the overlay (when click-through is off), use the tray icon, or
press Ctrl+Alt+O to open the settings window. Drag with the left mouse button
to move the overlay; size/position can be edited in the settings window.

Platform notes
--------------
Windows (primary target):
    Uses ``-transparentcolor`` for a chroma-key background, ``-alpha`` for
    opacity, ``WS_EX_TRANSPARENT`` for click-through and native tray/hotkey
    support through ctypes (standard library only).

macOS:
    ``-transparentcolor`` does not exist. Tk 8.6 understands
    ``wm_attributes("-transparent", True)`` with a ``systemTransparent``
    background (attempted below), but per-character transparency and smooth
    anti-aliasing are limited. A composition-friendly alternative is to use a
    very low ``-alpha`` and a matching dark background, or to render into a
    per-pixel-alpha image with a native helper. The global hotkey/tray code is
    Windows-only; right-click the overlay instead (keep click-through off).

Linux/X11:
    Tk cannot chroma-key the window. With a compositing window manager the
    ``-alpha`` opacity still works, but the background remains a solid colour.
    For true transparency use a compositor plus an ARGB visual via a toolkit
    that exposes it (not plain Tk), or use XShape. The tray/hotkey support is
    Windows-only; right-click the overlay instead.

Limitations:
    The chroma-key approach leaves faint anti-aliasing fringes around glyphs
    because those pixels are a blend of text and key colour. A near-black key
    keeps the fringes subtle. Full per-pixel alpha is not available in plain
    Tk on Windows.

Settings are stored next to this script as ``overlay_settings.json`` (or in
``%APPDATA%/AsciiOverlay`` when the script directory is read-only).
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import colorchooser, font as tkfont, ttk

# ---------------------------------------------------------------------------
# Constants, settings and small helpers
# ---------------------------------------------------------------------------

APP_NAME = "ASCII Overlay"
CONFIG_FILENAME = "overlay_settings.json"
TRANSPARENT_KEY = "#010203"  # chroma-key colour; intentionally almost black
CHAR_RAMP = " .:-=+*#%@"     # 10 brightness levels, dark -> bright
IS_WINDOWS = sys.platform.startswith("win")
FPS = 30


def clamp(value: float, low: float, high: float) -> float:
    """Return *value* limited to the inclusive range [low, high]."""
    return low if value < low else high if value > high else value


def smoothstep(t: float) -> float:
    """Classic hermite ease used by the value-noise interpolation."""
    return t * t * (3.0 - 2.0 * t)


def _hash01(ix: int, iy: int, seed: int) -> float:
    """Deterministic pseudo-random value in [0, 1] for an integer lattice."""
    n = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    n ^= n >> 16
    return (n & 0xFFFFFF) / 16777215.0


def value_noise(x: float, y: float, seed: int = 0) -> float:
    """Cheap, smooth 2D value noise in [0, 1] (no external dependencies)."""
    x0, y0 = math.floor(x), math.floor(y)
    fx, fy = x - x0, y - y0
    u, v = smoothstep(fx), smoothstep(fy)
    n00 = _hash01(x0, y0, seed)
    n10 = _hash01(x0 + 1, y0, seed)
    n01 = _hash01(x0, y0 + 1, seed)
    n11 = _hash01(x0 + 1, y0 + 1, seed)
    nx0 = n00 + (n10 - n00) * u
    nx1 = n01 + (n11 - n01) * u
    return nx0 + (nx1 - nx0) * v


def flow_angle(x: float, y: float, t: float, seed: int) -> float:
    """Smooth time-varying direction field used for fish and fireflies."""
    fx = value_noise(x * 0.055, y * 0.075 + t * 0.30, seed) - 0.5
    fy = value_noise(x * 0.055 + 51.3, y * 0.075 - t * 0.30, seed + 7) - 0.5
    return math.atan2(fy, fx)


def angle_delta(target: float, current: float) -> float:
    """Shortest signed angular difference, in (-pi, pi]."""
    return (target - current + math.pi) % math.tau - math.pi


def put_max(grid: list[list[float]], x: int, y: int, value: float) -> None:
    """Write *value* into the grid using a max blend (keeps highlights)."""
    if value <= 0.0:
        return
    if 0 <= y < len(grid):
        row = grid[y]
        if 0 <= x < len(row):
            if value > 1.0:
                value = 1.0
            if value > row[x]:
                row[x] = value


def fill_disk(grid: list[list[float]], cx: float, cy: float,
              radius: float, value: float) -> None:
    """Draw a soft-edged filled circle of brightness *value*."""
    if radius <= 0.0 or value <= 0.0:
        return
    r2 = radius * radius
    min_y = max(0, int(math.floor(cy - radius)))
    max_y = min(len(grid) - 1, int(math.ceil(cy + radius)))
    width = len(grid[0]) if grid else 0
    min_x = max(0, int(math.floor(cx - radius)))
    max_x = min(width - 1, int(math.ceil(cx + radius)))
    for y in range(min_y, max_y + 1):
        dy = y - cy
        for x in range(min_x, max_x + 1):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                # Slightly dim the rim so the body does not look like a block.
                falloff = 1.0 - 0.25 * (d2 / r2)
                put_max(grid, x, y, value * falloff)


def draw_sprite(grid: list[list[float]], sprite: list[list[float]],
                ox: int, oy: int, mirror: bool = False) -> None:
    """Stamp a small brightness sprite at (ox, oy), optionally mirrored."""
    height = len(sprite)
    width = len(sprite[0]) if height else 0
    for sy in range(height):
        row = sprite[sy]
        for sx in range(width):
            value = row[width - 1 - sx] if mirror else row[sx]
            if value > 0.0:
                put_max(grid, ox + sx, oy + sy, value)


@dataclass
class Settings:
    """All persisted application state."""

    animation: str = "Butterflies"
    opacity: float = 1.0
    speed: float = 1.0
    font_size: int = 14
    color: str = "#A6F2C1"
    click_through: bool = False
    always_on_top: bool = True
    x: int = 100
    y: int = 100
    width: int = 960
    height: int = 540

    @classmethod
    def for_screen(cls, screen_w: int, screen_h: int) -> "Settings":
        """Return defaults sized and centred for the current screen."""
        width = min(980, max(480, int(screen_w * 0.56)))
        height = min(560, max(320, int(screen_h * 0.52)))
        return cls(
            x=max(0, (screen_w - width) // 2),
            y=max(0, (screen_h - height) // 2),
            width=width,
            height=height,
        )

    @classmethod
    def load(cls, path: Path, screen_w: int, screen_h: int) -> "Settings":
        """Load settings, falling back to defaults for missing/invalid keys."""
        base = cls.for_screen(screen_w, screen_h)
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(raw, dict):
            return base

        def as_int(key: str, default: int, low: int, high: int) -> int:
            try:
                return int(clamp(int(raw.get(key, default)), low, high))
            except (TypeError, ValueError):
                return default

        def as_float(key: str, default: float, low: float, high: float) -> float:
            try:
                return float(clamp(float(raw.get(key, default)), low, high))
            except (TypeError, ValueError):
                return default

        animation = raw.get("animation", base.animation)
        if animation not in ANIMATION_NAMES:
            animation = base.animation
        color = raw.get("color", base.color)
        if (not isinstance(color, str)
                or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                or color.lower() == TRANSPARENT_KEY.lower()):
            color = base.color

        return cls(
            animation=animation,
            opacity=as_float("opacity", base.opacity, 0.1, 1.0),
            speed=as_float("speed", base.speed, 0.1, 3.0),
            font_size=as_int("font_size", base.font_size, 6, 32),
            color=color,
            click_through=raw.get("click_through") is True,
            always_on_top=raw.get("always_on_top") is not False,
            x=as_int("x", base.x, -20000, 20000),
            y=as_int("y", base.y, -20000, 20000),
            width=as_int("width", base.width, 120, 10000),
            height=as_int("height", base.height, 80, 10000),
        )

    def save(self, path: Path) -> str | None:
        """Atomically persist settings. Returns an error message or None."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(asdict(self), indent=2),
                            encoding="utf-8")
            os.replace(temp, path)
            return None
        except OSError as exc:
            return str(exc)


def config_path() -> Path:
    """Choose a writable JSON location, preferring the script directory."""
    folder = Path(__file__).resolve().parent
    candidate = folder / CONFIG_FILENAME
    if candidate.exists() or os.access(folder, os.W_OK):
        return candidate
    fallback = Path(os.environ.get("APPDATA") or Path.home()) / "AsciiOverlay"
    return fallback / CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Animations
# ---------------------------------------------------------------------------

class Animation:
    """
    Base class for every animation.

    Subclasses implement ``update(dt)`` to advance state and ``render(grid)``
    to paint brightness values into a row-major grid. Values are in [0, 1];
    zero means "transparent/space", one means the densest ramp character.
    """

    name = "Animation"
    cell_aspect = 2.0  # cell_height / cell_width, set by the app for accuracy

    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.time = 0.0
        self.reset()

    @property
    def row_scale(self) -> float:
        """Scale vertical cell movement so motion looks screen-space even."""
        return 1.0 / max(0.8, self.cell_aspect)

    def resize(self, width: int, height: int) -> None:
        """Reinitialise the animation when the character grid changes."""
        width, height = max(1, width), max(1, height)
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        self.reset()

    def reset(self) -> None:
        """Reset animation state (called on creation and resize)."""

    def update(self, dt: float) -> None:
        """Advance animation by *dt* seconds (already speed-adjusted)."""
        self.time += dt

    def render(self, grid: list[list[float]]) -> None:
        """Paint the current state into *grid*."""
        raise NotImplementedError


class Butterflies(Animation):
    """2-3 frame flapping sprites drifting along sine paths."""

    name = "Butterflies"

    # Three 7x4 frames: wings up, wings level, wings down.
    SPRITES = [
        [
            [0.20, 0.75, 0.45, 1.00, 0.45, 0.75, 0.20],
            [0.00, 0.55, 0.90, 1.00, 0.90, 0.55, 0.00],
            [0.00, 0.15, 0.40, 0.80, 0.40, 0.15, 0.00],
            [0.00, 0.00, 0.00, 0.45, 0.00, 0.00, 0.00],
        ],
        [
            [0.55, 0.85, 0.30, 0.00, 0.30, 0.85, 0.55],
            [0.35, 0.90, 0.50, 1.00, 0.50, 0.90, 0.35],
            [0.00, 0.30, 0.20, 0.80, 0.20, 0.30, 0.00],
            [0.00, 0.00, 0.00, 0.45, 0.00, 0.00, 0.00],
        ],
        [
            [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00],
            [0.15, 0.70, 0.40, 1.00, 0.40, 0.70, 0.15],
            [0.55, 0.90, 0.50, 0.80, 0.50, 0.90, 0.55],
            [0.20, 0.30, 0.10, 0.45, 0.10, 0.30, 0.20],
        ],
    ]

    def reset(self) -> None:
        self._rng = random.Random(7)
        count = max(2, min(6, self.width // 25))
        self.flies = []
        for _ in range(count):
            direction = self._rng.choice((-1, 1))
            self.flies.append({
                "x": self._rng.uniform(0, max(1, self.width - 1)),
                "base_y": self._rng.uniform(2, max(3, self.height - 6)),
                "amp": self._rng.uniform(1.5, 4.5),
                "freq": self._rng.uniform(0.5, 1.4),
                "phase": self._rng.uniform(0, math.tau),
                "speed": self._rng.uniform(2.5, 6.5) * direction,
                "drift": self._rng.uniform(0.05, 0.25),
            })

    def update(self, dt: float) -> None:
        super().update(dt)
        for fly in self.flies:
            fly["x"] += fly["speed"] * dt
            if fly["x"] > self.width + 3:
                fly["x"] = -4.0
                fly["base_y"] = self._rng.uniform(2, max(3, self.height - 6))
            elif fly["x"] < -4:
                fly["x"] = self.width + 3.0
                fly["base_y"] = self._rng.uniform(2, max(3, self.height - 6))

    def render(self, grid: list[list[float]]) -> None:
        for fly in self.flies:
            y = (fly["base_y"]
                 + fly["amp"] * math.sin(self.time * fly["freq"] + fly["phase"])
                 + 0.8 * math.sin(self.time * 2.1 * fly["drift"] + fly["phase"]))
            frame = int(self.time * 12.0 + fly["phase"] * 2.0) % len(self.SPRITES)
            sprite = self.SPRITES[frame]
            draw_sprite(grid, sprite, int(round(fly["x"])) - 3,
                        int(round(y)) - 2, mirror=fly["speed"] < 0)


class KoiPond(Animation):
    """Koi steered by a smooth noise field, with fading tails and ripples."""

    name = "Koi Pond"

    def reset(self) -> None:
        self._rng = random.Random(1234)
        count = max(3, min(7, self.width // 22))
        self.fish = []
        for index in range(count):
            self.fish.append({
                "x": self._rng.uniform(0, max(1, self.width - 1)),
                "y": self._rng.uniform(0, max(1, self.height - 1)),
                "heading": self._rng.uniform(0, math.tau),
                "speed": self._rng.uniform(5.0, 10.0),
                "seed": 17 + index * 41,
                "phase": self._rng.uniform(0, math.tau),
                "trail": deque(maxlen=28),
                "ripple_in": self._rng.uniform(0.8, 3.0),
            })
        self.ripples: list[dict] = []

    def update(self, dt: float) -> None:
        super().update(dt)
        width, height = self.width, self.height
        for fish in self.fish:
            # Smooth flow field plus a little body wiggle for life.
            desired = flow_angle(fish["x"], fish["y"], self.time, fish["seed"])
            turn = angle_delta(desired, fish["heading"])
            fish["heading"] += clamp(turn, -2.2 * dt, 2.2 * dt)
            fish["heading"] += math.sin(self.time * 2.3 + fish["phase"]) * 0.35 * dt

            new_x = fish["x"] + math.cos(fish["heading"]) * fish["speed"] * dt
            new_y = (fish["y"] + math.sin(fish["heading"]) * fish["speed"]
                     * dt * self.row_scale)
            if not (0 <= new_x < width and 0 <= new_y < height):
                fish["trail"].clear()  # avoid a streak across the wrap
            fish["x"] = new_x % width
            fish["y"] = new_y % height
            fish["trail"].appendleft((fish["x"], fish["y"]))

            fish["ripple_in"] -= dt
            if fish["ripple_in"] <= 0.0:
                self.ripples.append({
                    "x": fish["x"],
                    "y": fish["y"],
                    "age": 0.0,
                    "life": self._rng.uniform(1.8, 3.0),
                })
                fish["ripple_in"] = self._rng.uniform(1.4, 4.0)

        for ripple in self.ripples:
            ripple["age"] += dt
        self.ripples = [r for r in self.ripples if r["age"] < r["life"]]

    def render(self, grid: list[list[float]]) -> None:
        # Ripples first so fish bodies sit on top of them.
        for ripple in self.ripples:
            progress = ripple["age"] / ripple["life"]
            radius = 1.0 + 9.0 * progress
            brightness = 0.30 * (1.0 - progress) ** 1.6
            steps = max(18, int(radius * 7))
            for step in range(steps):
                angle = step / steps * math.tau
                x = ripple["x"] + math.cos(angle) * radius
                y = ripple["y"] + math.sin(angle) * radius * self.row_scale
                put_max(grid, int(round(x)), int(round(y)), brightness)
            if progress < 0.35:
                put_max(grid, int(round(ripple["x"])),
                        int(round(ripple["y"])), brightness * 1.5)

        for fish in self.fish:
            trail = list(fish["trail"])
            if not trail:
                continue
            length = len(trail)
            for index, (x, y) in enumerate(trail):
                u = index / max(1, length - 1)
                if index == 0:
                    radius, brightness = 1.0, 1.0
                elif index == 1:
                    radius, brightness = 1.25, 0.92
                else:
                    radius = max(0.0, 1.25 * (1.0 - u) ** 0.65)
                    brightness = 0.10 + 0.72 * (1.0 - u) ** 1.5
                fill_disk(grid, x, y, radius, brightness)

            # Small fins near the front and a translucent tail fan.
            if length > 3:
                hx, hy = trail[2]
                px = math.cos(fish["heading"] + math.pi / 2) * 1.4
                py = math.sin(fish["heading"] + math.pi / 2) * 1.4 * self.row_scale
                put_max(grid, int(round(hx + px)), int(round(hy + py)), 0.45)
                put_max(grid, int(round(hx - px)), int(round(hy - py)), 0.45)
                tx, ty = trail[-1]
                tail_x = math.cos(fish["heading"] + math.pi / 2) * 1.7
                tail_y = math.sin(fish["heading"] + math.pi / 2) * 1.7 * self.row_scale
                put_max(grid, int(round(tx + tail_x)), int(round(ty + tail_y)), 0.30)
                put_max(grid, int(round(tx - tail_x)), int(round(ty - tail_y)), 0.30)


class Rain(Animation):
    """Windless rain streaks with small gravity-driven splash particles."""

    name = "Rain"

    def reset(self) -> None:
        self._rng = random.Random(99)
        self.drops = [self._new_drop(initial=True)
                      for _ in range(max(40, self.width * self.height // 22))]
        self.splashes: list[dict] = []

    def _new_drop(self, initial: bool = False) -> dict:
        return {
            "x": self._rng.uniform(0, max(1, self.width)),
            "y": (self._rng.uniform(-self.height, 0) if initial
                  else self._rng.uniform(-10, -1)),
            "speed": self._rng.uniform(16.0, 34.0),
            "length": self._rng.randint(1, 3),
            "brightness": self._rng.uniform(0.35, 0.9),
        }

    def update(self, dt: float) -> None:
        super().update(dt)
        ground = self.height - 1
        for index, drop in enumerate(self.drops):
            drop["y"] += drop["speed"] * dt
            if drop["y"] >= ground:
                if self._rng.random() < 0.55:
                    particles = [
                        (self._rng.uniform(-8.0, 8.0),
                         self._rng.uniform(-9.0, -3.0))
                        for _ in range(self._rng.randint(3, 6))
                    ]
                    self.splashes.append({
                        "x": drop["x"],
                        "y": ground,
                        "age": 0.0,
                        "life": 0.42,
                        "parts": particles,
                    })
                self.drops[index] = self._new_drop()

        for splash in self.splashes:
            splash["age"] += dt
        self.splashes = [s for s in self.splashes if s["age"] < s["life"]]

    def render(self, grid: list[list[float]]) -> None:
        width = self.width
        for drop in self.drops:
            x = int(drop["x"]) % width
            y = int(drop["y"])
            for offset in range(drop["length"]):
                brightness = drop["brightness"] * (1.0 - 0.3 * offset)
                put_max(grid, x, y - offset, brightness)

        for splash in self.splashes:
            age = splash["age"]
            progress = age / splash["life"]
            brightness = 0.65 * (1.0 - progress) ** 1.4
            for vx, vy in splash["parts"]:
                x = splash["x"] + vx * age
                y = splash["y"] + vy * age + 20.0 * age * age
                put_max(grid, int(round(x)), int(round(y)), brightness)


class Fireflies(Animation):
    """Slowly wandering motes with soft, blinking halos."""

    name = "Fireflies"

    def reset(self) -> None:
        self._rng = random.Random(2024)
        count = max(10, min(44, self.width * self.height // 140))
        self.flies = []
        for index in range(count):
            self.flies.append({
                "x": self._rng.uniform(0, max(1, self.width - 1)),
                "y": self._rng.uniform(0, max(1, self.height - 1)),
                "heading": self._rng.uniform(0, math.tau),
                "speed": self._rng.uniform(1.2, 3.0),
                "phase": self._rng.uniform(0, math.tau),
                "blink": self._rng.uniform(1.0, 2.6),
                "seed": 71 + index * 23,
            })

    def update(self, dt: float) -> None:
        super().update(dt)
        for fly in self.flies:
            desired = flow_angle(fly["x"] * 1.6, fly["y"] * 1.6,
                                 self.time * 0.7, fly["seed"])
            turn = angle_delta(desired, fly["heading"])
            fly["heading"] += clamp(turn, -1.2 * dt, 1.2 * dt)
            fly["heading"] += math.sin(self.time * 1.7 + fly["phase"]) * 0.5 * dt
            fly["x"] = (fly["x"] + math.cos(fly["heading"])
                        * fly["speed"] * dt) % self.width
            fly["y"] = (fly["y"] + math.sin(fly["heading"])
                        * fly["speed"] * dt * self.row_scale) % self.height

    def render(self, grid: list[list[float]]) -> None:
        for fly in self.flies:
            pulse = max(0.0, math.sin(self.time * fly["blink"] + fly["phase"]))
            brightness = 0.06 + 0.94 * pulse ** 3
            x = int(round(fly["x"]))
            y = int(round(fly["y"]))
            put_max(grid, x, y, brightness)
            halo = brightness * 0.32
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                put_max(grid, x + dx, y + dy, halo)
            diagonal = brightness * 0.10
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                put_max(grid, x + dx, y + dy, diagonal)


class Snow(Animation):
    """Falling snowflakes with sway, depth (brightness) and size variation."""

    name = "Snow"

    def reset(self) -> None:
        self._rng = random.Random(555)
        count = max(24, min(220, self.width * self.height // 45))
        self.flakes = []
        for _ in range(count):
            self.flakes.append({
                "x": self._rng.uniform(0, max(1, self.width)),
                "y": self._rng.uniform(-self.height, self.height),
                "speed": self._rng.uniform(2.0, 9.0),
                "size": self._rng.choice((0, 0, 0, 1, 1, 2)),
                "phase": self._rng.uniform(0, math.tau),
                "sway": self._rng.uniform(0.4, 2.2),
                "sway_rate": self._rng.uniform(0.4, 1.3),
                "brightness": self._rng.uniform(0.5, 1.0),
            })

    def update(self, dt: float) -> None:
        super().update(dt)
        for flake in self.flakes:
            flake["y"] += flake["speed"] * dt
            flake["x"] += (0.7 + math.sin(self.time * flake["sway_rate"]
                                          + flake["phase"]) * flake["sway"]) * dt
            if flake["y"] > self.height + 1:
                flake["y"] = -1.0
                flake["x"] = self._rng.uniform(0, max(1, self.width))
            if flake["x"] > self.width:
                flake["x"] -= self.width
            elif flake["x"] < 0:
                flake["x"] += self.width

    def render(self, grid: list[list[float]]) -> None:
        for flake in self.flakes:
            x = int(round(flake["x"]))
            y = int(round(flake["y"]))
            brightness = flake["brightness"]
            size = flake["size"]
            if size == 0:
                put_max(grid, x, y, brightness)
            elif size == 1:
                put_max(grid, x, y, brightness)
                halo = brightness * 0.32
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    put_max(grid, x + dx, y + dy, halo)
            else:
                put_max(grid, x, y, brightness)
                halo = brightness * 0.48
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    put_max(grid, x + dx, y + dy, halo)
                dim = brightness * 0.18
                for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    put_max(grid, x + dx, y + dy, dim)


class OceanWaves(Animation):
    """Layered undulating wave crests with foam, glints and water texture."""

    name = "Ocean Waves"

    def reset(self) -> None:
        self.layers = []
        for index in range(6):
            self.layers.append({
                "base": 0.55 + index * 0.075,
                "amp": 1.0 + index * 0.55,
                "freq": 0.050 + index * 0.011,
                "speed": 0.8 + index * 0.22,
                "phase": index * 1.7,
                "bright": 0.22 + index * 0.12,
                "foam": index >= 3,
            })

    def render(self, grid: list[list[float]]) -> None:
        width, height = self.width, self.height
        for index, layer in enumerate(self.layers):
            base_y = height * layer["base"]
            for x in range(width):
                wave = math.sin(x * layer["freq"] + self.time * layer["speed"]
                                + layer["phase"])
                y = int(round(base_y + layer["amp"] * wave))
                brightness = layer["bright"] * (0.7 + 0.3 * wave)
                put_max(grid, x, y, brightness)
                put_max(grid, x, y + 1, brightness * 0.32)
                if wave > 0.72:
                    put_max(grid, x, y - 1, min(1.0, brightness * 1.7))
                if layer["foam"] and wave > 0.1 and (x * 7 + index * 13
                                                     + int(self.time * 9)) % 19 == 0:
                    put_max(grid, x, y - 1, min(1.0, brightness * 2.2))

        # A sparse shimmer texture below the crest lines implies depth.
        start = max(0, int(height * 0.72))
        for y in range(start, height):
            for x in range(width):
                n = value_noise(x * 0.12, y * 0.42 + self.time * 0.35, seed=5)
                if n > 0.78:
                    put_max(grid, x, y, 0.06 + 0.18 * (n - 0.78) / 0.22)


class Starfield(Animation):
    """Parallax stars twinkling behind slowly drifting noise clouds."""

    name = "Starfield & Clouds"

    def reset(self) -> None:
        self._rng = random.Random(808)
        count = max(25, min(180, self.width * self.height // 30))
        self.stars = []
        for _ in range(count):
            self.stars.append({
                "x": self._rng.uniform(0, max(1, self.width)),
                "y": self._rng.uniform(0, max(1, self.height)),
                "depth": self._rng.uniform(0.25, 1.0),
                "phase": self._rng.uniform(0, math.tau),
                "rate": self._rng.uniform(0.8, 3.2),
                "brightness": self._rng.uniform(0.45, 1.0),
            })
        self.clouds = []
        for index in range(3):
            self.clouds.append({
                "x": self._rng.uniform(0, max(1, self.width)),
                "y": self._rng.uniform(self.height * 0.10, self.height * 0.55),
                "rx": self._rng.uniform(self.width * 0.18, self.width * 0.32),
                "ry": self._rng.uniform(max(2.0, self.height * 0.10),
                                        max(3.0, self.height * 0.19)),
                "speed": self._rng.uniform(0.4, 1.2) * self._rng.choice((-1, 1)),
                "seed": 31 + index * 19,
            })

    def update(self, dt: float) -> None:
        super().update(dt)
        for star in self.stars:
            star["x"] = (star["x"] + star["depth"] * 1.8 * dt) % self.width
            star["y"] = (star["y"] + star["depth"] * 0.5 * dt
                         * self.row_scale) % self.height
        for cloud in self.clouds:
            cloud["x"] += cloud["speed"] * dt
            if cloud["speed"] > 0 and cloud["x"] - cloud["rx"] > self.width:
                cloud["x"] = -cloud["rx"]
            elif cloud["speed"] < 0 and cloud["x"] + cloud["rx"] < 0:
                cloud["x"] = self.width + cloud["rx"]

    def render(self, grid: list[list[float]]) -> None:
        width, height = self.width, self.height

        def paint_cloud(cloud: dict) -> None:
            cx, cy = cloud["x"], cloud["y"]
            rx, ry = cloud["rx"], cloud["ry"]
            x0 = max(0, int(cx - rx))
            x1 = min(width - 1, int(cx + rx))
            y0 = max(0, int(cy - ry))
            y1 = min(height - 1, int(cy + ry))
            for y in range(y0, y1 + 1):
                ny = (y - cy) / max(0.1, ry)
                for x in range(x0, x1 + 1):
                    nx = (x - cx) / max(0.1, rx)
                    falloff = 1.0 - nx * nx - ny * ny
                    if falloff <= 0.0:
                        continue
                    n = value_noise(x * 0.11 + cloud["seed"] * 10.0,
                                    y * 0.22 - self.time * 0.03, cloud["seed"])
                    # A continuous, softly textured body rather than isolated
                    # noise speckles: falloff defines the cloud, noise adds detail.
                    value = (falloff ** 0.75) * (0.05 + 0.22 * n)
                    if value > 0.012:
                        put_max(grid, x, y, min(0.38, value))

        # Paint clouds on both sides of the wrap seam twice so they slide
        # across the edge without popping.
        for cloud in self.clouds:
            paint_cloud(cloud)
            shifted = dict(cloud)
            if cloud["speed"] > 0:
                shifted["x"] += width
            else:
                shifted["x"] -= width
            paint_cloud(shifted)

        for star in self.stars:
            twinkle = 0.55 + 0.45 * math.sin(self.time * star["rate"]
                                             + star["phase"])
            brightness = star["brightness"] * (0.35 + 0.65 * twinkle * twinkle)
            x = int(round(star["x"]))
            y = int(round(star["y"]))
            put_max(grid, x, y, brightness)
            if star["depth"] > 0.72:
                halo = brightness * 0.28
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    put_max(grid, x + dx, y + dy, halo)


# Registry used by the settings UI and persisted JSON.
ANIMATIONS: dict[str, type[Animation]] = {
    cls.name: cls
    for cls in (Butterflies, KoiPond, Rain, Fireflies, Snow, OceanWaves, Starfield)
}
ANIMATION_NAMES = tuple(ANIMATIONS)

# ---------------------------------------------------------------------------
# Windows integration: tray icon, global hotkey and extended window styles
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM
    )

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        # NOTIFYICONDATA_V2 layout is accepted by all supported Windows versions
        # and avoids the GUID / balloon-icon tail of V3/V4.
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND
    if hasattr(_user32, "GetWindowLongPtrW"):
        _get_window_long = _user32.GetWindowLongPtrW
        _set_window_long = _user32.SetWindowLongPtrW
        _long_ptr_type = ctypes.c_ssize_t
    else:  # 32-bit Python
        _get_window_long = _user32.GetWindowLongW
        _set_window_long = _user32.SetWindowLongW
        _long_ptr_type = ctypes.c_long
    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = _long_ptr_type
    _set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, _long_ptr_type]
    _set_window_long.restype = _long_ptr_type

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _user32.RegisterHotKey.argtypes = [
        wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
    ]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wintypes.HICON
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW),
    ]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    # Window style / positioning constants.
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x08000000
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020

    # Tray / hotkey constants.
    WM_DESTROY = 0x0002
    WM_QUIT = 0x0012
    WM_HOTKEY = 0x0312
    WM_CONTEXTMENU = 0x007B
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_APP = 0x8000
    NIM_ADD = 0
    NIM_DELETE = 2
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    HOTKEY_ID = 1
    VK_O = 0x4F


class WindowsIntegration:
    """
    Native tray icon and global hotkey, implemented with ctypes only.

    A small hidden window and its Win32 message loop live on a daemon thread.
    User actions are forwarded to Tk through a thread-safe queue which the app
    polls with ``after()``. This keeps Tk single-threaded while still allowing
    a system-wide hotkey and a tray icon.
    """

    HOTKEY_LABEL = "Ctrl+Alt+O"

    def __init__(self, events: "queue.Queue[str]") -> None:
        self.events = events
        self.ready = threading.Event()
        self.error = ""
        self.tray_active = False
        self.hotkey_active = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._stopped = False
        self._tray_callback = 0

        if not IS_WINDOWS:
            self.error = "Tray icon and global hotkey are Windows-only."
            self.ready.set()
            return

        self._thread = threading.Thread(target=self._run,
                                        name="ascii-overlay-win32",
                                        daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout=2.5):
            self.error = "Windows integration did not start in time."

    # -- thread -----------------------------------------------------------
    def _wndproc(self, hwnd, message, wparam, lparam):
        """Message-only window procedure for tray clicks and hotkeys."""
        try:
            if message == self._tray_callback:
                event = int(lparam) & 0xFFFF
                if event in (WM_RBUTTONUP, WM_LBUTTONDBLCLK, WM_CONTEXTMENU):
                    self.events.put("settings")
                    return 0
            elif message == WM_HOTKEY and int(wparam) == HOTKEY_ID:
                self.events.put("settings")
                return 0
            elif message == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        hwnd = None
        icon = None
        nid = None
        class_name = f"AsciiOverlayTray_{os.getpid()}_{id(self)}"
        registered_class = False
        try:
            self._thread_id = _kernel32.GetCurrentThreadId()
            instance = _kernel32.GetModuleHandleW(None)
            callback = _WNDPROC(self._wndproc)
            self._wndproc_ref = callback  # keep the callback alive
            self._tray_callback = WM_APP + 1

            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.lpfnWndProc = callback
            wc.hInstance = instance
            wc.lpszClassName = class_name
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(),
                              "RegisterClassExW failed")
            registered_class = True

            # A hidden top-level window (rather than HWND_MESSAGE) is the most
            # compatible target for Shell_NotifyIcon across Windows versions.
            hwnd = _user32.CreateWindowExW(
                0, class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, instance, None
            )
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")

            icon = _user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            nid.uCallbackMessage = self._tray_callback
            nid.hIcon = icon
            nid.szTip = "ASCII Overlay - right-click for settings"
            self.tray_active = bool(
                _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            )
            if not self.tray_active:
                self.error = "The tray icon could not be created."

            if _user32.RegisterHotKey(
                hwnd, HOTKEY_ID,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_O
            ):
                self.hotkey_active = True
            else:
                extra = f"Global hotkey {self.HOTKEY_LABEL} is unavailable."
                self.error = (self.error + " " + extra).strip()

            self.ready.set()

            msg = wintypes.MSG()
            while not self._stopped:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # pragma: no cover - platform specific
            self.error = f"Windows integration unavailable: {exc}"
        finally:
            self.ready.set()
            try:
                if hwnd:
                    if self.tray_active and nid is not None:
                        _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                    _user32.UnregisterHotKey(hwnd, HOTKEY_ID)
                    _user32.DestroyWindow(hwnd)
                if registered_class:
                    _user32.UnregisterClassW(class_name, instance)
            except Exception:
                pass

    # -- public -----------------------------------------------------------
    def stop(self) -> None:
        """Ask the helper thread to exit and clean up the tray icon."""
        if self._stopped:
            return
        self._stopped = True
        if not IS_WINDOWS:
            return
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1.5)


class WindowStyler:
    """Applies click-through/tool-window/topmost styles to the overlay HWND."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._hwnd = None

    def _handle(self):
        if self._hwnd:
            return self._hwnd
        self.root.update_idletasks()
        window_id = self.root.winfo_id()
        parent = _user32.GetParent(window_id)
        self._hwnd = parent or window_id
        return self._hwnd

    def apply(self, click_through: bool, topmost: bool) -> None:
        if not IS_WINDOWS:
            return
        try:
            hwnd = self._handle()
            style = int(_get_window_long(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
            style &= ~WS_EX_APPWINDOW
            style |= WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
            if click_through:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            _set_window_long(hwnd, GWL_EXSTYLE, style)

            flags = (SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                     | SWP_FRAMECHANGED)
            # -1 = HWND_TOPMOST, -2 = HWND_NOTOPMOST
            _user32.SetWindowPos(
                hwnd, wintypes.HWND(-1 if topmost else -2),
                0, 0, 0, 0, flags
            )
        except Exception as exc:  # pragma: no cover - platform specific
            print(f"Warning: could not apply window styles: {exc}",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# Application: transparent overlay, animation loop and settings UI
# ---------------------------------------------------------------------------

class OverlayApp:
    """Owns the Tk overlay, the animation timer and the settings window."""

    def __init__(self) -> None:
        self._closing = False
        self._resources_stopped = False
        self._after_id = None
        self._event_after_id = None
        self._grid_after_id = None
        self._save_after_id = None
        self._last_frame: float | None = None
        self._next_frame = 0.0
        self._grid_size = (0, 0)
        self._cell_aspect = 2.0
        self._char_w = 8
        self._line_h = 16
        self._row_text: list[str] = []
        self.row_items: list[int] = []
        self.grid: list[list[float]] = []
        self.animation: Animation | None = None
        self.settings_win: tk.Toplevel | None = None
        self.status_label: tk.Label | None = None
        self._drag_offset: tuple[int, int] | None = None
        self._anim_error_reported = False
        self._settings_built = False

        # -- root window -------------------------------------------------
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.config_path = config_path()
        self.settings = Settings.load(self.config_path, screen_w, screen_h)

        self._bg_color = TRANSPARENT_KEY
        self._configure_transparency()

        try:
            self.root.configure(bg=self._bg_color)
        except tk.TclError:
            self._bg_color = TRANSPARENT_KEY
            self.root.configure(bg=self._bg_color)

        try:
            self.canvas = tk.Canvas(self.root, bg=self._bg_color,
                                    highlightthickness=0, bd=0, takefocus=0)
        except tk.TclError:
            # macOS can reject systemTransparent on some Tk builds.
            self._bg_color = TRANSPARENT_KEY
            self.canvas = tk.Canvas(self.root, bg=self._bg_color,
                                    highlightthickness=0, bd=0, takefocus=0)
        self.canvas.pack(fill="both", expand=True)

        self._font_family = self._pick_font_family()
        self.font = tkfont.Font(root=self.root, family=self._font_family,
                                size=-max(6, self.settings.font_size))

        self.root.geometry(self._geometry_string())
        try:
            self.root.attributes("-topmost", bool(self.settings.always_on_top))
        except tk.TclError:
            pass
        self._apply_opacity()
        try:
            self.root.attributes("-toolwindow", True)
        except tk.TclError:
            pass  # non-Windows Tk builds may not know this attribute

        self.root.update_idletasks()
        self.styler = WindowStyler(self.root)
        self.styler.apply(self.settings.click_through,
                          self.settings.always_on_top)

        # -- rendering and animation ------------------------------------
        self._build_grid()
        self._set_animation(self.settings.animation)
        self._bind_events()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        # -- tray icon / global hotkey ----------------------------------
        self.events: "queue.Queue[str]" = queue.Queue()
        self.integration = WindowsIntegration(self.events)
        if (IS_WINDOWS and self.settings.click_through
                and not (self.integration.tray_active
                         or self.integration.hotkey_active)):
            # Never leave the user with an unreachable, click-through window.
            self.settings.click_through = False
            self.styler.apply(False, self.settings.always_on_top)

        self.root.after(10, self._tick)
        self._event_after_id = self.root.after(120, self._poll_events)
        # Tk occasionally rewrites the wrapper's extended style while the
        # window is first mapped, so apply the Win32 styles once more.
        self.root.after(300, lambda: self.styler.apply(
            self.settings.click_through, self.settings.always_on_top))
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")

    # -- window setup -----------------------------------------------------
    def _pick_font_family(self) -> str:
        """Pick a common monospace family available on the platform."""
        available = set(tkfont.families(self.root))
        for candidate in ("Cascadia Mono", "Consolas", "Lucida Console",
                          "Courier New", "DejaVu Sans Mono", "Liberation Mono",
                          "Menlo", "Monaco"):
            if candidate in available:
                return candidate
        return tkfont.nametofont("TkFixedFont").actual("family")

    def _configure_transparency(self) -> None:
        """Enable chroma-key transparency where the platform supports it."""
        if IS_WINDOWS:
            try:
                self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
            except tk.TclError as exc:
                print(f"Warning: transparentcolor unavailable: {exc}",
                      file=sys.stderr)
        elif sys.platform == "darwin":
            # Best effort on macOS; see the module docstring for caveats.
            try:
                self.root.wm_attributes("-transparent", True)
                self._bg_color = "systemTransparent"
            except tk.TclError:
                pass

    def _geometry_string(self) -> str:
        s = self.settings
        return f"{s.width}x{s.height}{s.x:+d}{s.y:+d}"

    def _apply_opacity(self) -> None:
        try:
            self.root.attributes("-alpha",
                                 clamp(float(self.settings.opacity), 0.1, 1.0))
        except tk.TclError as exc:
            print(f"Warning: window alpha unavailable: {exc}", file=sys.stderr)

    # -- events -----------------------------------------------------------
    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<ButtonPress-3>", self._open_settings_event)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Hotkey fallback when the overlay happens to have focus. The real
        # system-wide hotkey is registered by WindowsIntegration.
        self.root.bind_all("<Control-Alt-Key-o>", self._open_settings_event)

    def _open_settings_event(self, _event=None) -> str:
        self.show_settings()
        return "break"

    def _drag_start(self, event: tk.Event) -> None:
        try:
            self._drag_offset = (event.x_root - self.root.winfo_x(),
                                 event.y_root - self.root.winfo_y())
        except tk.TclError:
            self._drag_offset = None

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_offset is None:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.settings.x, self.settings.y = int(x), int(y)
        try:
            self.root.geometry(f"{int(x):+d}{int(y):+d}")
        except tk.TclError:
            pass

    def _drag_end(self, _event: tk.Event) -> None:
        self._drag_offset = None
        self._sync_geometry()
        self._schedule_save()
        if self.settings_win is not None:
            self._sync_geometry_fields()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if event.width < 2 or event.height < 2:
            return
        self.settings.width = int(event.width)
        self.settings.height = int(event.height)
        self._schedule_grid_rebuild()
        self._schedule_save()

    # -- animation loop ---------------------------------------------------
    def _tick(self) -> None:
        self._after_id = None
        if self._closing:
            return
        now = time.perf_counter()
        dt = 0.0 if self._last_frame is None else now - self._last_frame
        self._last_frame = now
        dt = min(dt, 0.06)  # avoid huge jumps after a stall

        if self.animation is not None and self.grid:
            self.animation.update(dt * max(0.05, self.settings.speed))
            blank = [0.0] * self._grid_size[0]
            for row in self.grid:
                row[:] = blank
            try:
                self.animation.render(self.grid)
            except Exception as exc:  # keep animating if one frame fails
                if not self._anim_error_reported:
                    print(f"Animation error: {exc}", file=sys.stderr)
                    self._anim_error_reported = True
            self._draw_grid()

        # Fixed-cadence scheduling compensates for redraw time and Windows
        # timer granularity, keeping the loop close to FPS.
        frame_time = 1.0 / FPS
        if self._next_frame <= 0.0:
            self._next_frame = time.perf_counter()
        self._next_frame += frame_time
        delay = self._next_frame - time.perf_counter()
        if delay <= 0.001:  # we fell behind; restart the cadence cleanly
            self._next_frame = time.perf_counter() + frame_time
            delay = frame_time
        self._after_id = self.root.after(max(1, int(delay * 1000.0)),
                                         self._tick)

    def _draw_grid(self) -> None:
        """Convert brightness values to characters, updating changed rows."""
        step = len(CHAR_RAMP) - 1
        for index, row in enumerate(self.grid):
            chars = "".join(
                CHAR_RAMP[min(step, max(0, int(value * step + 0.5)))]
                for value in row
            ).rstrip()
            if chars != self._row_text[index]:
                self.canvas.itemconfigure(self.row_items[index], text=chars)
                self._row_text[index] = chars

    def _build_grid(self) -> None:
        """Recreate the character grid after a resize or font change."""
        try:
            self.root.update_idletasks()
        except tk.TclError:
            return
        view_w = self.canvas.winfo_width()
        view_h = self.canvas.winfo_height()
        if view_w < 2:
            view_w = max(2, self.settings.width)
        if view_h < 2:
            view_h = max(2, self.settings.height)

        self._char_w = max(1, self.font.measure("M"))
        self._line_h = max(1, self.font.metrics("linespace"))
        cols = max(1, view_w // self._char_w)
        rows = max(1, view_h // self._line_h)
        self._cell_aspect = self._line_h / self._char_w
        self._grid_size = (cols, rows)

        for item in self.row_items:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass
        self.row_items = [
            self.canvas.create_text(0, row * self._line_h, anchor="nw",
                                    text="", font=self.font,
                                    fill=self.settings.color)
            for row in range(rows)
        ]
        self.grid = [[0.0] * cols for _ in range(rows)]
        self._row_text = [""] * rows

        if self.animation is not None:
            self.animation.cell_aspect = self._cell_aspect
            self.animation.resize(cols, rows)

    def _set_animation(self, name: str) -> None:
        animation_cls = ANIMATIONS.get(name)
        if animation_cls is None:
            animation_cls = next(iter(ANIMATIONS.values()))
        self.animation = animation_cls(*self._grid_size)
        self.animation.cell_aspect = self._cell_aspect
        self.settings.animation = animation_cls.name
        # Clear the canvas immediately; otherwise rows that become empty in
        # the new animation would keep showing text from the old one.
        for item in self.row_items:
            try:
                self.canvas.itemconfigure(item, text="")
            except tk.TclError:
                pass
        self._row_text = [""] * self._grid_size[1]
        self._anim_error_reported = False

    # -- deferred work ----------------------------------------------------
    def _schedule_grid_rebuild(self) -> None:
        if self._grid_after_id is not None:
            try:
                self.root.after_cancel(self._grid_after_id)
            except tk.TclError:
                pass
        self._grid_after_id = self.root.after(110, self._rebuild_grid)

    def _rebuild_grid(self) -> None:
        self._grid_after_id = None
        if not self._closing:
            self._build_grid()

    def _schedule_save(self) -> None:
        if self._save_after_id is not None:
            try:
                self.root.after_cancel(self._save_after_id)
            except tk.TclError:
                pass
        self._save_after_id = self.root.after(700, self._save_settings)

    # -- persistence / events --------------------------------------------
    def _sync_geometry(self) -> None:
        try:
            self.settings.x = int(self.root.winfo_x())
            self.settings.y = int(self.root.winfo_y())
            self.settings.width = int(self.root.winfo_width())
            self.settings.height = int(self.root.winfo_height())
        except tk.TclError:
            pass

    def _save_settings(self) -> None:
        self._save_after_id = None
        self._sync_geometry()
        error = self.settings.save(self.config_path)
        if error:
            self._set_status(f"Could not save settings: {error}", error=True)
        elif self.settings_win is not None:
            self._set_status(f"Saved to {self.config_path}")

    def _poll_events(self) -> None:
        self._event_after_id = None
        if self._closing:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "settings":
                self.show_settings()
        self._event_after_id = self.root.after(100, self._poll_events)

    def _on_root_destroy(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._shutdown_resources()

    def _shutdown_resources(self) -> None:
        if self._resources_stopped:
            return
        self._resources_stopped = True
        try:
            self.integration.stop()
        except Exception:
            pass

    def quit(self) -> None:
        if self._closing:
            return
        self._save_settings()
        self._closing = True
        for after_id in (self._after_id, self._event_after_id,
                         self._grid_after_id, self._save_after_id):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._shutdown_resources()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _set_status(self, message: str, error: bool = False) -> None:
        if self.status_label is not None:
            try:
                self.status_label.configure(
                    text=message, foreground="#B00020" if error else "#3A6B47"
                )
            except tk.TclError:
                pass

    def run(self) -> None:
        self.root.mainloop()

    # -- settings window --------------------------------------------------
    def show_settings(self) -> None:
        """Show, build or raise the settings window."""
        if self._closing:
            return
        if self.settings_win is None or not self.settings_win.winfo_exists():
            self._build_settings_window()
        self._sync_settings_ui()
        assert self.settings_win is not None
        self.settings_win.deiconify()
        self.settings_win.lift()
        try:
            self.settings_win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.settings_win.after(60, self.settings_win.focus_force)

    def _hide_settings(self) -> None:
        self._save_settings()
        if self.settings_win is not None:
            try:
                self.settings_win.withdraw()
            except tk.TclError:
                pass

    def _build_settings_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - Settings")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._hide_settings)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.bind("<Escape>", lambda _e: self._hide_settings())
        win.bind("<Control-Key-s>", lambda _e: self._save_settings())
        self.settings_win = win

        try:
            style = ttk.Style(win)
            if IS_WINDOWS and "vista" in style.theme_names():
                style.theme_use("vista")
        except tk.TclError:
            pass

        # Tk variables are created once and reused when the window is shown.
        self.anim_var = tk.StringVar(value=self.settings.animation)
        self.opacity_var = tk.DoubleVar(value=self.settings.opacity)
        self.speed_var = tk.DoubleVar(value=self.settings.speed)
        self.font_var = tk.DoubleVar(value=self.settings.font_size)
        self.color_var = tk.StringVar(value=self.settings.color)
        self.click_var = tk.BooleanVar(value=self.settings.click_through)
        self.top_var = tk.BooleanVar(value=self.settings.always_on_top)
        self.x_var = tk.StringVar(value=str(self.settings.x))
        self.y_var = tk.StringVar(value=str(self.settings.y))
        self.w_var = tk.StringVar(value=str(self.settings.width))
        self.h_var = tk.StringVar(value=str(self.settings.height))

        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text=APP_NAME,
                  font=("Segoe UI", 15, "bold")).grid(
            row=0, column=0, sticky="w")
        if IS_WINDOWS and self.integration.hotkey_active:
            hint = ("Right-click the overlay or press Ctrl+Alt+O to reopen "
                    "this window.")
        else:
            hint = "Right-click the overlay to reopen this window."
        ttk.Label(outer, text=hint, foreground="#666666").grid(
            row=1, column=0, sticky="w", pady=(1, 8))

        # Animation picker -------------------------------------------------
        ttk.Label(outer, text="Animation").grid(
            row=2, column=0, sticky="w", pady=(6, 2))
        self.anim_combo = ttk.Combobox(
            outer, textvariable=self.anim_var, values=list(ANIMATION_NAMES),
            state="readonly", width=30)
        self.anim_combo.grid(row=3, column=0, sticky="ew")
        self.anim_combo.bind("<<ComboboxSelected>>", self._on_animation_change)

        # Sliders ----------------------------------------------------------
        sliders = ttk.Frame(outer)
        sliders.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        sliders.columnconfigure(1, weight=1)

        self.opacity_value = self._add_slider(
            sliders, 0, "Opacity", self.opacity_var, 0.1, 1.0,
            self._on_opacity)
        self.speed_value = self._add_slider(
            sliders, 1, "Animation speed", self.speed_var, 0.1, 3.0,
            self._on_speed)
        self.font_value = self._add_slider(
            sliders, 2, "Font size / density", self.font_var, 6, 32,
            self._on_font_size)

        # Colour -----------------------------------------------------------
        colour_row = ttk.Frame(outer)
        colour_row.grid(row=5, column=0, sticky="ew", pady=(8, 2))
        ttk.Label(colour_row, text="Text colour").pack(side="left")
        self.color_swatch = tk.Button(
            colour_row, width=3, command=self._pick_color, relief="flat",
            bd=0, highlightthickness=1, highlightbackground="#777777",
            cursor="hand2")
        self.color_swatch.pack(side="left", padx=(10, 8))
        self.color_hex = ttk.Label(colour_row, text=self.settings.color)
        self.color_hex.pack(side="left")
        ttk.Button(colour_row, text="Choose...",
                   command=self._pick_color).pack(side="right")

        # Overlay options --------------------------------------------------
        options = ttk.LabelFrame(outer, text="Overlay", padding=10)
        options.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        click_check = ttk.Checkbutton(options, text="Click-through",
                                     variable=self.click_var,
                                     command=self._on_click_through)
        click_check.grid(row=0, column=0, sticky="w")
        if not IS_WINDOWS:
            click_check.state(["disabled"])
        ttk.Checkbutton(options, text="Always on top",
                        variable=self.top_var,
                        command=self._on_always_on_top).grid(
            row=0, column=1, sticky="w", padx=(24, 0))
        click_note = (
            "When click-through is on, mouse input goes to the desktop. "
            "Use the hotkey/tray or this window to turn it off."
            if IS_WINDOWS else
            "Click-through is implemented on Windows only; it needs native "
            "input shaping on macOS/Linux."
        )
        ttk.Label(options, text=click_note, foreground="#666666",
                  wraplength=420).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Window geometry --------------------------------------------------
        geometry = ttk.LabelFrame(outer, text="Position and size",
                                  padding=10)
        geometry.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        entries = (
            ("X", self.x_var, 0, 0), ("Y", self.y_var, 0, 2),
            ("W", self.w_var, 1, 0), ("H", self.h_var, 1, 2),
        )
        for label, variable, row, column in entries:
            ttk.Label(geometry, text=label).grid(
                row=row, column=column, sticky="e", padx=(0, 4), pady=2)
            entry = ttk.Entry(geometry, textvariable=variable, width=8)
            entry.grid(row=row, column=column + 1, sticky="w", pady=2)
            entry.bind("<Return>", lambda _e: self._apply_geometry_fields())

        buttons = ttk.Frame(geometry)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Apply",
                   command=self._apply_geometry_fields).pack(side="left")
        ttk.Button(buttons, text="Center",
                   command=self._center_overlay).pack(side="left", padx=6)
        ttk.Label(geometry,
                  text="Tip: drag the overlay with the left mouse button.",
                  foreground="#666666").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # Footer -----------------------------------------------------------
        footer = ttk.Frame(outer)
        footer.grid(row=8, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(footer, text="Save now",
                   command=lambda: self._save_settings()).pack(side="left")
        ttk.Button(footer, text="Hide",
                   command=self._hide_settings).pack(side="left", padx=6)
        ttk.Button(footer, text="Quit",
                   command=self.quit).pack(side="right")

        # Tray/hotkey availability note.
        note_parts = []
        if self.integration.hotkey_active:
            note_parts.append("Global hotkey: Ctrl+Alt+O")
        if self.integration.tray_active:
            note_parts.append("Tray icon active")
        if self.integration.error:
            note_parts.append(self.integration.error)
        if not IS_WINDOWS:
            note_parts.append("Global hotkey/tray are Windows-only")
        ttk.Label(outer, text="  |  ".join(note_parts),
                  foreground="#666666", wraplength=430).grid(
            row=9, column=0, sticky="w", pady=(8, 0))

        self.status_label = tk.Label(
            outer, text=f"Settings file: {self.config_path}",
            anchor="w", justify="left", wraplength=430,
            foreground="#3A6B47")
        self.status_label.grid(row=10, column=0, sticky="ew", pady=(5, 0))

        self._settings_built = True
        self._sync_settings_ui()

        win.update_idletasks()
        width = max(450, win.winfo_reqwidth())
        height = win.winfo_reqheight()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        win.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}"
                     f"+{max(0, (screen_h - height) // 2)}")
        win.minsize(width, height)

    def _add_slider(self, parent: ttk.Frame, row: int, label: str,
                    variable: tk.DoubleVar, low: float, high: float,
                    command) -> ttk.Label:
        """Add a labelled slider row and return its value label."""
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        scale = ttk.Scale(parent, from_=low, to=high, variable=variable,
                          command=lambda _v: command())
        scale.grid(row=row, column=1, sticky="ew", pady=3)
        value_label = ttk.Label(parent, width=6, anchor="e")
        value_label.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=3)
        return value_label

    def _sync_settings_ui(self) -> None:
        if not self._settings_built:
            return
        self.anim_var.set(self.settings.animation)
        self.opacity_var.set(self.settings.opacity)
        self.speed_var.set(self.settings.speed)
        self.font_var.set(self.settings.font_size)
        self.color_var.set(self.settings.color)
        self.click_var.set(self.settings.click_through)
        self.top_var.set(self.settings.always_on_top)
        self.opacity_value.configure(text=f"{self.settings.opacity:.2f}")
        self.speed_value.configure(text=f"{self.settings.speed:.2f}")
        self.font_value.configure(text=str(self.settings.font_size))
        self._update_color_swatch()
        self._sync_geometry_fields()
        self._set_status(f"Settings file: {self.config_path}")

    def _sync_geometry_fields(self) -> None:
        if not self._settings_built:
            return
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            width, height = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            x, y = self.settings.x, self.settings.y
            width, height = self.settings.width, self.settings.height
        self.x_var.set(str(x))
        self.y_var.set(str(y))
        self.w_var.set(str(width))
        self.h_var.set(str(height))

    def _update_color_swatch(self) -> None:
        if not self._settings_built:
            return
        try:
            self.color_swatch.configure(bg=self.settings.color,
                                        activebackground=self.settings.color)
            self.color_hex.configure(text=self.settings.color)
        except tk.TclError:
            pass

    # -- settings callbacks ----------------------------------------------
    def _on_animation_change(self, _event=None) -> None:
        name = self.anim_var.get()
        if name not in ANIMATIONS:
            return
        if name != self.settings.animation:
            self._set_animation(name)
        self._schedule_save()

    def _on_opacity(self) -> None:
        value = clamp(float(self.opacity_var.get()), 0.1, 1.0)
        self.settings.opacity = value
        self._apply_opacity()
        self.opacity_value.configure(text=f"{value:.2f}")
        self._schedule_save()

    def _on_speed(self) -> None:
        value = clamp(float(self.speed_var.get()), 0.1, 3.0)
        self.settings.speed = value
        self.speed_value.configure(text=f"{value:.2f}")
        self._schedule_save()

    def _on_font_size(self) -> None:
        size = int(round(float(self.font_var.get())))
        size = int(clamp(size, 6, 32))
        self.font_value.configure(text=str(size))
        if size == self.settings.font_size:
            return
        self.settings.font_size = size
        try:
            self.font.configure(size=-size)
        except tk.TclError:
            return
        self._schedule_grid_rebuild()
        self._schedule_save()

    def _pick_color(self) -> None:
        _rgb, colour = colorchooser.askcolor(
            color=self.settings.color, parent=self.settings_win,
            title="Choose overlay text colour")
        if not colour:
            return
        if colour.lower() == TRANSPARENT_KEY.lower():
            self._set_status("That colour matches the transparent key; "
                             "choose a different shade.", error=True)
            return
        self.settings.color = colour
        for item in self.row_items:
            try:
                self.canvas.itemconfigure(item, fill=colour)
            except tk.TclError:
                pass
        self._update_color_swatch()
        self._schedule_save()

    def _on_click_through(self) -> None:
        self.settings.click_through = bool(self.click_var.get())
        self._apply_window_options()
        self._set_status(
            "Click-through enabled - use Ctrl+Alt+O or the tray icon."
            if self.settings.click_through else "Click-through disabled.")
        self._schedule_save()

    def _on_always_on_top(self) -> None:
        self.settings.always_on_top = bool(self.top_var.get())
        self._apply_window_options()
        self._schedule_save()

    def _apply_window_options(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.settings.always_on_top))
        except tk.TclError:
            pass
        self.styler.apply(self.settings.click_through,
                          self.settings.always_on_top)

    def _apply_geometry_fields(self) -> None:
        try:
            x = int(self.x_var.get())
            y = int(self.y_var.get())
            width = int(clamp(int(self.w_var.get()), 120, 10000))
            height = int(clamp(int(self.h_var.get()), 80, 10000))
        except (TypeError, ValueError):
            self._set_status("X, Y, W and H must be whole numbers.",
                             error=True)
            return
        x = max(-20000, min(20000, x))
        y = max(-20000, min(20000, y))
        self.settings.x, self.settings.y = x, y
        self.settings.width, self.settings.height = width, height
        try:
            self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        except tk.TclError as exc:
            self._set_status(f"Could not apply geometry: {exc}", error=True)
            return
        self.root.update_idletasks()
        self._sync_geometry()
        self._sync_geometry_fields()
        self._schedule_grid_rebuild()
        self._schedule_save()
        self._set_status("Window position and size applied.")

    def _center_overlay(self) -> None:
        self.root.update_idletasks()
        width = max(120, self.root.winfo_width())
        height = max(80, self.root.winfo_height())
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.settings.width, self.settings.height = width, height
        self.settings.x, self.settings.y = x, y
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.update_idletasks()
        self._sync_geometry_fields()
        self._schedule_save()
        self._set_status("Overlay centred on the primary screen.")


def main() -> None:
    app = OverlayApp()
    app.run()


if __name__ == "__main__":
    main()
