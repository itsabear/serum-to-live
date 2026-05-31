# Serum to Live

Converts **Serum 2** presets (`.SerumPreset`) into **Ableton-compatible** `.vstpreset` files, ready to drag straight into your Live sets.

## What it does

Serum 2 presets saved from version 2.0.x aren't directly loadable in Ableton's preset browser for Serum 2.1.4. This app translates the internal format so all your presets — including wavetable, multisample, granular, and spectral oscillators — show up and sound correct in Live.

## Requirements

- macOS
- Python 3.10+
- Serum 2 installed (the app reads from its default preset location)

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 app.py
```

## Usage

1. **Folder mode** — point to a folder of `.SerumPreset` files (defaults to Serum 2's preset library). Use **Convert New Only** to skip presets already converted.
2. **Individual files** — pick specific presets or drag them onto the window.
3. Choose an output folder (defaults to `~/Desktop/Serum Vstpresets`).
4. Hit **Convert All** or **Convert New Only**.

Drag a folder or `.SerumPreset` files directly onto the window to get started quickly.

## Output

Converted presets preserve the original folder structure by default. Enable **Flat output** to put everything in one folder. The output `.vstpreset` files can be loaded in Ableton Live via the browser under Plug-ins → Serum 2.
