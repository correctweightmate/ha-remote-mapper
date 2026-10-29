# Changelog

## 1.0.0 — 2026-10-29

- First release.
- Press-to-assign editor: tap the pencil, press a button on the remote, choose what it does, tap Done.
- Sidebar panel (admins) plus a `custom:remote-mapper-card` Lovelace card with a visual editor.
- Works with any device that exposes `event` entities (Matter, Hue, Z2M/MQTT event entities…).
- Dedicated layout for the IKEA BILRESA scroll wheel: 3 channels × scroll up/down, press, double, triple, hold. Scroll steps scale with the number of detents.
- Actions: toggle, on, off, brighten, dim, set brightness, scene, script, play/pause, volume up/down, custom action (any action + JSON data).
- `remote_mapper_button` event on every press, for your own automations.
- Edit-mode switch per remote that turns itself off after inactivity (default 10 min).
- Diagnostics download.
