#!/usr/bin/env python3
"""
Serum preset → Ableton .vstpreset (Serum 2.1.4) converter.
Supports both Serum 2 .SerumPreset and Serum 1 .fxp formats.
All conversion logic lives here; the UI imports convert_one and find_*.
"""

import sys, struct, json, hashlib, copy, re, zlib
from pathlib import Path

try:
    import cbor2
except ImportError:
    raise ImportError("Missing: pip3 install cbor2")
try:
    import zstandard
except ImportError:
    raise ImportError("Missing: pip3 install zstandard")

# ── constants ─────────────────────────────────────────────────────────────────

SERUM2_CLASS_ID = b"56534558667350736572756D20320000"

INFO_XML = (
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b"<MetaInfo>"
    b"<Attribute id='MediaType' value='VstPreset' type='string' flags='writeProtected'></Attribute>"
    b"<Attribute id='PlugInCategory' value='Instrument|Synth' type='string' flags='writeProtected'></Attribute>"
    b"<Attribute id='plugtype' value='Instrument|Synth' type='string' flags='writeProtected'></Attribute>"
    b"<Attribute id='PlugInName' value='Serum 2' type='string' flags='writeProtected'></Attribute>"
    b"<Attribute id='plugname' value='Serum 2' type='string' flags='writeProtected'></Attribute>"
    b"<Attribute id='PlugInVendor' value='Xfer Records' type='string' flags='writeProtected'></Attribute>"
    b"</MetaInfo>"
)

_HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
TEMPLATE_VST = str(_HERE / "Serum Preset.vstpreset")
INIT_CACHE   = str(_HERE / "init_state_cache.cbor")

# ── Serum 1 asset roots ───────────────────────────────────────────────────────
_S1_ROOT    = Path("/Library/Audio/Presets/Xfer Records/Serum Presets")
_S1_TABLES  = _S1_ROOT / "Tables"
_S1_NOISES  = _S1_ROOT / "Noises"
_S2_TABLES  = Path("/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Tables")

# ── codec helpers ─────────────────────────────────────────────────────────────

_dctx = zstandard.ZstdDecompressor()
_cctx = zstandard.ZstdCompressor(level=11)

def _decomp(chunk: bytes) -> bytes:
    js  = struct.unpack_from("<I", chunk, 9)[0]
    st  = chunk[17 + js:]
    dsz = struct.unpack_from("<I", st, 0)[0]
    return _dctx.decompress(st[8:], max_output_size=dsz * 2 + 65536)

def _build_xfer(meta: dict, cbor_obj: object) -> bytes:
    raw  = cbor2.dumps(cbor_obj)
    comp = _cctx.compress(raw)
    m    = dict(meta)
    m["hash"] = hashlib.md5(comp).hexdigest()
    j    = json.dumps(m, separators=(",", ":")).encode()
    st   = struct.pack("<I", len(raw)) + struct.pack("<I", 2) + comp
    return b"XferJson\x00" + struct.pack("<I", len(j)) + b"\x00\x00\x00\x00" + j + st

def _assemble(comp: bytes, cont: bytes) -> bytes:
    H  = 48
    co = H
    no = co + len(comp)
    io = no + len(cont)
    lo = io + len(INFO_XML)
    hdr = b"VST3" + struct.pack("<I", 1) + SERUM2_CLASS_ID + struct.pack("<q", lo)
    lst = (
        b"List" + struct.pack("<I", 3)
        + b"Comp" + struct.pack("<q", co) + struct.pack("<q", len(comp))
        + b"Cont" + struct.pack("<q", no) + struct.pack("<q", len(cont))
        + b"Info" + struct.pack("<q", io) + struct.pack("<q", len(INFO_XML))
    )
    return hdr + comp + cont + INFO_XML + lst

def _load_vstpreset_chunks(path: Path) -> dict:
    data = path.read_bytes()
    lo   = struct.unpack_from("<q", data, 40)[0]
    ld   = data[lo:]
    n    = struct.unpack_from("<I", ld, 4)[0]
    out  = {}
    for i in range(n):
        b   = 8 + i * 20
        cid = ld[b:b+4].decode("ascii")
        off = struct.unpack_from("<q", ld, b+4)[0]
        sz  = struct.unpack_from("<q", ld, b+12)[0]
        out[cid] = data[off:off+sz]
    return out

# ── SFZ path normalisation (2.0.x → 2.1.4 uses // separator) ─────────────────

def _fix_sfz_paths(sfz_text: str) -> str:
    def _fix(m):
        path = m.group(1).strip().replace("\\", "/").replace("/", "//", 1)
        return "sample=" + path
    return re.sub(r"sample=(.+)", _fix, sfz_text)

def _fix_files_keys(files: dict) -> dict:
    return {
        k.replace("\\", "/").replace("/", "//", 1): v
        for k, v in files.items()
    }

# ── oscillator-type inference ──────────────────────────────────────────────────

# In 2.0.x, kParamType is absent when the oscillator uses the default WT type.
# We infer the correct type from which sub-oscillator has non-default plainParams.
_OSC_SUB_TO_TYPE = [
    ("WTOsc0",          "kOsc_WT"),
    ("MultiSampleOsc0", "kOsc_MultiSample"),
    ("SpectralOsc0",    "kOsc_Spectral"),
    ("GranularOsc0",    "kOsc_Granular"),
    ("SampleOsc0",      "kOsc_Sample"),
]

def _infer_osc_type(sec_20x: dict):
    for sub_key, osc_type in _OSC_SUB_TO_TYPE:
        sub = sec_20x.get(sub_key)
        if isinstance(sub, dict):
            pp = sub.get("plainParams")
            if isinstance(pp, dict) and pp:
                return osc_type
    return None

# ── keys to drop ──────────────────────────────────────────────────────────────

_DROP_20X_KEYS = frozenset({
    "ClipPlayer", "Filter", "GranularOsc", "MultiSampleOsc", "Osc",
    "SerumGUI", "SpectralOsc", "WTOsc",
    "arpBankDisplayName", "clipBankDisplayName",
    "fileType", "presetAuthor", "presetDescription", "presetName",
})
_DROP_SUB_KEYS = frozenset({"name"})

# ── merge logic ───────────────────────────────────────────────────────────────

def _merge_section(sec_20x: dict, sec_orch: dict, sec_init: dict) -> dict:
    """
    Merge a 2.0.x section using ORCH as structural base.

    plainParams strategy:
      - "default" or absent  → {} (let Serum use its defaults), or {kParamType}
                               if we can infer the oscillator type
      - dict with values     → use 2.0.x values directly (no ORCH bleed-through),
                               inject inferred kParamType when absent

    Sub-sections are merged recursively. embedded_sfz and files get
    path-separator normalisation (2.0.x backslash → 2.1.4 double-slash).
    """
    if not isinstance(sec_20x, dict):
        return sec_20x

    pp     = sec_20x.get("plainParams")
    result = copy.deepcopy(sec_orch) if isinstance(sec_orch, dict) else {}

    for k, v in sec_20x.items():
        if k in _DROP_SUB_KEYS:
            continue

        if k == "plainParams":
            if v == "default" or v is None:
                inferred = _infer_osc_type(sec_20x)
                result["plainParams"] = {"kParamType": inferred} if inferred else {}
            elif isinstance(v, dict):
                vv = dict(v)
                if "kParamType" not in vv:
                    inferred = _infer_osc_type(sec_20x)
                    if inferred:
                        vv["kParamType"] = inferred
                result["plainParams"] = vv
            else:
                result["plainParams"] = v

        elif k == "embedded_sfz" and isinstance(v, str):
            result[k] = _fix_sfz_paths(v)

        elif k == "files" and isinstance(v, dict):
            result[k] = _fix_files_keys(v)

        elif isinstance(v, dict):
            orch_sub = sec_orch.get(k, {}) if isinstance(sec_orch, dict) else {}
            init_sub = sec_init.get(k, {}) if isinstance(sec_init, dict) else {}
            result[k] = _merge_section(v, orch_sub, init_sub)

        else:
            result[k] = v

    return result


def _convert_cbor(obj_20x: dict, obj_orch: dict, obj_init: dict) -> dict:
    """Produce a 2.1.4 CBOR state from 2.0.x, using ORCH as structural base."""
    result = copy.deepcopy(obj_orch)

    for k, v in obj_20x.items():
        if k in _DROP_20X_KEYS or k not in result:
            continue
        if isinstance(v, dict):
            result[k] = _merge_section(v, obj_orch.get(k, {}), obj_init.get(k, {}))
        elif not isinstance(result[k], dict):
            result[k] = v

    result.update({
        "component":      "processor",
        "product":        "Serum2",
        "productVersion": "2.1.4",
        "url":            "https://xferrecords.com/",
        "vendor":         "Xfer Records",
        "version":        10.0,
    })
    for k in ("lockOversampling", "lockTuning", "mpeConfig", "mpeEnabled", "mpePitchBendRange"):
        if k in obj_20x:
            result[k] = obj_20x[k]
    if "tags" in obj_20x:
        result["tags"] = obj_20x["tags"]

    return result

# ── template state (loaded once) ──────────────────────────────────────────────

class _Templates:
    _inst = None

    def __init__(self):
        chunks = _load_vstpreset_chunks(Path(TEMPLATE_VST))

        comp_chunk = chunks["Comp"]
        js  = struct.unpack_from("<I", comp_chunk, 9)[0]
        self.comp_meta = json.loads(comp_chunk[17:17+js])
        self.obj_orch  = cbor2.loads(_decomp(comp_chunk))

        cont_chunk = chunks["Cont"]
        js2 = struct.unpack_from("<I", cont_chunk, 9)[0]
        self.cont_meta = json.loads(cont_chunk[17:17+js2])
        self.obj_cont  = cbor2.loads(_decomp(cont_chunk))

        self.obj_init  = cbor2.loads(Path(INIT_CACHE).read_bytes())

def _get_templates() -> _Templates:
    if _Templates._inst is None:
        _Templates._inst = _Templates()
    return _Templates._inst

# ── Serum 1 FXP converter ─────────────────────────────────────────────────────

# Serum 1 parameter index → (Serum 2 CBOR section, kParam key, scale, offset)
# s2_value = s1_normalized * scale + offset
# Section path uses '.' for nesting: "Oscillator0.WTOsc0" means
# obj["Oscillator0"]["WTOsc0"]["plainParams"][key]
_S1_PARAM_MAP = {
    # Global
    0:  ("Global0",             "kParamMasterVolume",  1,    0),
    # Oscillator A → Oscillator0
    # Volume/Warp/Phase: direct 0-1; Octave/Semi/Fine/Pan/Unison: display units
    1:  ("Oscillator0",         "kParamVolume",        1,    0),
    2:  ("Oscillator0",         "kParamPan",         100,  -50),   # -50..+50
    3:  ("Oscillator0",         "kParamOctave",        8,   -4),   # -4..+4 (round)
    4:  ("Oscillator0",         "kParamTranspose",    24,  -12),   # -12..+12 (round)
    5:  ("Oscillator0",         "kParamFine",        200, -100),   # -100..+100 cents
    6:  ("Oscillator0",         "kParamUnison",       16,    0),   # 0..16 (round)
    7:  ("Oscillator0",         "kParamDetune",        1,    0),
    8:  ("Oscillator0",         "kParamDetuneWid",   100,    0),   # 0..100%
    9:  ("Oscillator0.WTOsc0",  "kParamWarp",          1,    0),
    10: ("Oscillator0",         "kParamCoarsePit",     1,    0),
    11: ("Oscillator0.WTOsc0",  "kParamTablePos",    255,    1),   # 1..256
    12: ("Oscillator0.WTOsc0",  "kParamRandomPhase", 100,    0),   # 0..100%
    13: ("Oscillator0.WTOsc0",  "kParamInitialPhase",  1,    0),
    # Oscillator B → Oscillator1
    14: ("Oscillator1",         "kParamVolume",        1,    0),
    15: ("Oscillator1",         "kParamPan",         100,  -50),
    16: ("Oscillator1",         "kParamOctave",        8,   -4),
    17: ("Oscillator1",         "kParamTranspose",    24,  -12),
    18: ("Oscillator1",         "kParamFine",        200, -100),
    19: ("Oscillator1",         "kParamUnison",       16,    0),
    20: ("Oscillator1",         "kParamDetune",        1,    0),
    21: ("Oscillator1",         "kParamDetuneWid",   100,    0),
    22: ("Oscillator1.WTOsc0",  "kParamWarp",          1,    0),
    23: ("Oscillator1",         "kParamCoarsePit",     1,    0),
    24: ("Oscillator1.WTOsc0",  "kParamTablePos",    255,    1),
    25: ("Oscillator1.WTOsc0",  "kParamRandomPhase", 100,    0),
    26: ("Oscillator1.WTOsc0",  "kParamInitialPhase",  1,    0),
    # Noise → Oscillator2
    27: ("Oscillator2",         "kParamVolume",        1,    0),
    28: ("Oscillator2",         "kParamPitch",       200, -100),
    29: ("Oscillator2",         "kParamFine",          2,   -1),
    30: ("Oscillator2",         "kParamPan",         100,  -50),
    # Amplitude envelope (Serum 1 Env1 → Serum 2 Env0)
    35: ("Env0",                "kParamAttack",        1,    0),
    36: ("Env0",                "kParamHold",          1,    0),
    37: ("Env0",                "kParamDecay",         1,    0),
    38: ("Env0",                "kParamSustain",       1,    0),
    39: ("Env0",                "kParamRelease",       1,    0),
    # Filter (Freq 0-1 non-linear, pass through; Reso/Drive/Var 0-100)
    45: ("VoiceFilter0",        "kParamFreq",          1,    0),
    46: ("VoiceFilter0",        "kParamReso",        100,    0),
    47: ("VoiceFilter0",        "kParamDrive",       100,    0),
    48: ("VoiceFilter0",        "kParamVar",         100,    0),
    49: ("VoiceFilter0",        "kParamWet",         100,    0),
    # Envelope 2 (Serum 1 Env2 → Serum 2 Env1)
    51: ("Env1",                "kParamAttack",        1,    0),
    52: ("Env1",                "kParamHold",          1,    0),
    53: ("Env1",                "kParamDecay",         1,    0),
    54: ("Env1",                "kParamSustain",       1,    0),
    55: ("Env1",                "kParamRelease",       1,    0),
    # Envelope 3 (Serum 1 Env3 → Serum 2 Env2)
    56: ("Env2",                "kParamAttack",        1,    0),
    57: ("Env2",                "kParamHold",          1,    0),
    58: ("Env2",                "kParamDecay",         1,    0),
    59: ("Env2",                "kParamSustain",       1,    0),
    60: ("Env2",                "kParamRelease",       1,    0),
    # LFO rates
    61: ("LFO0",                "kParamRate",          1,    0),
    62: ("LFO1",                "kParamRate",          1,    0),
    63: ("LFO2",                "kParamRate",          1,    0),
    64: ("LFO3",                "kParamRate",          1,    0),
    # Portamento
    65: ("VoicePanel0",         "kParamPortamentoTime",1,   0),
    66: ("VoicePanel0",         "kParamPortamentoCurve",200,-100),
    # Oscillator enable flags
    212: ("Oscillator0",        "kParamEnable",        1,    0),
    213: ("Oscillator1",        "kParamEnable",        1,    0),
    216: ("VoiceFilter0",       "kParamEnable",        1,    0),
}

def _read_wav_frames(path: Path) -> tuple:
    """Return (total_frames, sample_rate) from a WAV file, parsing manually to handle float format."""
    data = path.read_bytes()
    if data[:4] != b'RIFF':
        return 0, 44100
    pos, ch, sr, bps = 12, 1, 44100, 32
    while pos < len(data) - 8:
        cid = data[pos:pos+4]
        csz = struct.unpack_from('<I', data, pos+4)[0]
        if cid == b'fmt ':
            ch  = struct.unpack_from('<H', data, pos+10)[0]
            sr  = struct.unpack_from('<I', data, pos+12)[0]
            bps = struct.unpack_from('<H', data, pos+22)[0]
        elif cid == b'data':
            return csz // (ch * bps // 8), sr
        pos += 8 + csz + (csz % 2)
    return 0, sr

def _resolve_wt(s1_path: str, is_noise: bool = False) -> tuple:
    """
    Resolve a Serum 1 wavetable/noise path to a Serum 2 relativePathToWT.
    Returns (s2_relative_path, num_frames, sample_rate).
    """
    if not s1_path:
        return None, 0, 44100
    s1_path = s1_path.replace("\\", "/").lstrip("/?")
    roots = [_S2_TABLES, _S1_NOISES if is_noise else _S1_TABLES]
    for root in roots:
        full = root / s1_path
        if full.exists():
            frames, sr = _read_wav_frames(full)
            return "/" + s1_path, frames, sr
    return "/" + s1_path, 0, 44100

def _read_s1_str(raw: bytes, offset: int, length: int = 512) -> str:
    """Read a null-terminated ASCII string from the FXP raw bytes."""
    chunk = raw[offset:offset+length]
    end = chunk.find(b'\x00')
    if end == -1:
        end = length
    return chunk[:end].decode('ascii', errors='ignore').strip()

def _parse_fxp(fxp_path: Path) -> tuple:
    """
    Parse a Serum 1 .fxp file.
    Returns (preset_name, author, description, params_list, wt_a, wt_b, noise).
    """
    data = fxp_path.read_bytes()
    if data[:4] != b'CcnK':
        raise ValueError("Not a VST2 FXP file")
    fxp_type = data[8:12]
    if fxp_type == b'FPCh':
        chunk_size = struct.unpack_from('>I', data, 56)[0]
        raw = zlib.decompress(data[60:60+chunk_size])
    else:
        raise ValueError(f"Unsupported FXP type: {fxp_type}")

    preset_name = data[28:56].rstrip(b'\x00').decode('utf-8', errors='ignore').strip()

    # 299 float32 parameters starting at 0x3460
    params = list(struct.unpack_from(f'<{min(299, (len(raw)-0x3460)//4)}f', raw, 0x3460))

    # Wavetable paths
    wt_a  = _read_s1_str(raw, 0x3c08)
    wt_b  = _read_s1_str(raw, 0x3e08)
    noise = _read_s1_str(raw, 0x4008)

    # Preset metadata stored in raw chunk
    raw_name = _read_s1_str(raw, 0x4972, 40)
    author   = _read_s1_str(raw, 0x49a0, 40)
    desc     = _read_s1_str(raw, 0x49c0, 64)
    if raw_name:
        preset_name = raw_name

    return preset_name, author, desc, params, wt_a, wt_b, noise

# FX definitions: (type_num, fx_key, enable_param, [(s1_param_idx, kparam, scale, offset)])
# scale/offset: s2_value = s1_normalized * scale + offset
# Only linear params ("=" in SYParameters.txt) are mapped; non-linear skipped
_S1_FX = [
    # Distortion (enable param 154)
    (0, "FXDistortion", 154, [
        (96, "kParamWet",   100, 0),
        (97, "kParamDrive", 100, 0),
    ]),
    # Flanger (enable 155)
    (1, "FXFlanger", 155, [
        (103, "kParamWet",      100, 0),
        (104, "kParamBeatSync",   1, 0),
        (106, "kParamDepth",    100, 0),
        (107, "kParamFeedback", 100, 0),
        (108, "kParamWidth",    360, 0),
    ]),
    # Phaser (enable 156)
    (2, "FXPhaser", 156, [
        (109, "kParamWet",      100, 0),
        (110, "kParamBeatSync",   1, 0),
        (112, "kParamDepth",    100, 0),
        (114, "kParamFeedback", 100, 0),
        (115, "kParamWidth",    360, 0),
    ]),
    # Chorus (enable 157)
    (3, "FXChorus", 157, [
        (116, "kParamWet",      100, 0),
        (117, "kParamBeatSync",   1, 0),
        (122, "kParamFeedback",   1, 0),
    ]),
    # Delay (enable 158)
    (4, "FXDelay", 158, [
        (124, "kParamWet",      100, 0),
        (127, "kParamBeatSync",   1, 0),
        (128, "kParamLink",       1, 0),
        (132, "kParamFeedback", 100, 0),
        (133, "kParamOffsetL",    1, 0),
        (134, "kParamOffsetR",    1, 0),
    ]),
    # Compressor (enable 159)
    (5, "FXComp", 159, [
        (140, "kParamMultiband",  1, 0),
    ]),
    # Reverb (enable 160)
    (6, "FXReverb", 160, [
        (81, "kParamWet",   100, 0),
        (82, "kParamSize",  100, 0),
        (87, "kParamWidth", 100, 0),
    ]),
    # EQ (enable 161)
    (7, "FXEQ", 161, [
        (90, "kParamReso1",  100,   0),
        (91, "kParamReso2",  100,   0),
        (92, "kParamGain1",   48, -24),
        (93, "kParamGain2",   48, -24),
    ]),
    # FX Filter (enable 162)
    (8, "FXFilter", 162, [
        (141, "kParamWet",   100, 0),
        (143, "kParamFreq",    1, 0),
        (144, "kParamReso",  100, 0),
        (145, "kParamDrive", 100, 0),
        (146, "kParamVar",   100, 0),
    ]),
    # Hyperspace (enable 163)
    (9, "FXHyperD", 163, [
        (147, "kParamWet",      100, 0),
        (149, "kParamDetune",   100, 0),
        (151, "kParamRetrig",     1, 0),
        (152, "kParamDimESize", 100, 0),
        (153, "kParamDimEWet",  100, 0),
    ]),
]

def _build_s1_fx(params: list) -> list:
    """Build the Serum 2 FX list from Serum 1 FXP parameters."""
    fx_list = []
    for fx_type, fx_key, enable_idx, param_map in _S1_FX:
        enabled = params[enable_idx] > 0.5 if enable_idx < len(params) else False
        pp = {"kParamEnable": 1.0 if enabled else 0.0}
        for s1_idx, kparam, scale, offset in param_map:
            if s1_idx < len(params):
                pp[kparam] = float(params[s1_idx]) * scale + offset
        fx_list.append({
            "type":           fx_type,
            "kUIParamMixOrGain": 0.0,
            fx_key:           {"plainParams": pp},
        })
    return fx_list

def _build_s1_cbor(params: list, wt_a: str, wt_b: str, noise: str, obj_init: dict) -> dict:
    """Build a Serum 2 processor CBOR from Serum 1 FXP data."""
    result = copy.deepcopy(obj_init)

    # Integer params (need rounding after scale)
    _INT_PARAMS = {"kParamOctave", "kParamTranspose", "kParamUnison"}

    # Apply mapped synthesis parameters
    for idx, (section_path, kparam, scale, offset) in _S1_PARAM_MAP.items():
        if idx >= len(params):
            continue
        val = float(params[idx]) * scale + offset
        if kparam in _INT_PARAMS:
            val = float(round(val))
        parts = section_path.split(".")
        node = result
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
        pp = node.setdefault("plainParams", {})
        if isinstance(pp, dict):
            pp[kparam] = val

    # Set wavetable oscillators
    for osc_key, wt_path, is_noise in [
        ("Oscillator0", wt_a,  False),
        ("Oscillator1", wt_b,  False),
        ("Oscillator2", noise, True),
    ]:
        if wt_path:
            s2_path, num_frames, sr = _resolve_wt(wt_path, is_noise)
            osc = result.setdefault(osc_key, {})
            wt  = osc.setdefault("WTOsc0", {})
            wt.update({
                "relativePathToWT": s2_path,
                "numFrames":        num_frames,
                "sampleRate":       sr,
                "numChannels":      1,
            })
            if "flex" not in wt:
                wt["flex"] = {}

    # Build FX list
    result.setdefault("FXRack0", {})["FX"] = _build_s1_fx(params)

    result.update({
        "component":      "processor",
        "product":        "Serum2",
        "productVersion": "2.1.4",
        "url":            "https://xferrecords.com/",
        "vendor":         "Xfer Records",
        "version":        10.0,
    })
    return result

# ── public API ────────────────────────────────────────────────────────────────

def convert_one(sp_path: Path, out_path: Path) -> None:
    """Convert a .SerumPreset or .fxp to .vstpreset. Raises on error."""
    if sp_path.suffix.lower() == ".fxp":
        return convert_one_fxp(sp_path, out_path)
    t = _get_templates()

    sp_data = sp_path.read_bytes()
    if sp_data[:8] != b"XferJson":
        raise ValueError("Not a Serum 2 preset (bad magic)")

    js      = struct.unpack_from("<I", sp_data, 9)[0]
    meta_20x = json.loads(sp_data[17:17+js])
    st       = sp_data[17+js:]
    dsz      = struct.unpack_from("<I", st, 0)[0]
    obj_20x  = cbor2.loads(_dctx.decompress(st[8:], max_output_size=dsz * 2 + 65536))

    # Build Comp chunk
    obj_214  = _convert_cbor(obj_20x, t.obj_orch, t.obj_init)
    comp     = _build_xfer(t.comp_meta, obj_214)

    # Build Cont chunk — update BOTH JSON header and CBOR body
    cont_obj = copy.deepcopy(t.obj_cont)
    cont_obj["presetName"]          = meta_20x.get("presetName", "")
    cont_obj["presetAuthor"]        = meta_20x.get("presetAuthor", "")
    cont_obj["presetDescription"]   = meta_20x.get("presetDescription", "")
    cont_obj["selectedPresetPath"]  = str(sp_path)
    cont_obj["presetHasBeenEdited"] = False

    cont_meta = dict(t.cont_meta)
    cont_meta["presetName"]        = meta_20x.get("presetName", "")
    cont_meta["presetAuthor"]      = meta_20x.get("presetAuthor", "")
    cont_meta["presetDescription"] = meta_20x.get("presetDescription", "")
    cont = _build_xfer(cont_meta, cont_obj)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_assemble(comp, cont))


def convert_one_fxp(fxp_path: Path, out_path: Path) -> None:
    """Convert a single Serum 1 .fxp to .vstpreset. Raises on error."""
    t = _get_templates()
    preset_name, author, desc, params, wt_a, wt_b, noise = _parse_fxp(fxp_path)

    obj_214 = _build_s1_cbor(params, wt_a, wt_b, noise, t.obj_init)
    comp    = _build_xfer(t.comp_meta, obj_214)

    cont_obj = copy.deepcopy(t.obj_cont)
    cont_obj["presetName"]          = preset_name
    cont_obj["presetAuthor"]        = author
    cont_obj["presetDescription"]   = desc
    cont_obj["selectedPresetPath"]  = str(fxp_path)
    cont_obj["presetHasBeenEdited"] = False

    cont_meta = dict(t.cont_meta)
    cont_meta["presetName"]        = preset_name
    cont_meta["presetAuthor"]      = author
    cont_meta["presetDescription"] = desc
    cont = _build_xfer(cont_meta, cont_obj)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_assemble(comp, cont))


def output_path_for(sp_path: Path, input_root: Path, output_root: Path, flat: bool) -> Path:
    if flat:
        return output_root / (sp_path.stem + ".vstpreset")
    rel = sp_path.relative_to(input_root)
    return output_root / rel.with_suffix(".vstpreset")


def find_presets(input_root: Path) -> list[Path]:
    s2 = sorted(input_root.rglob("*.SerumPreset"))
    s1 = sorted(input_root.rglob("*.fxp"))
    return s2 + s1


def find_new_presets(input_root: Path, output_root: Path, flat: bool) -> list[Path]:
    """Return only presets that don't already have a matching .vstpreset in output."""
    result = []
    for sp in find_presets(input_root):
        out = output_path_for(sp, input_root, output_root, flat)
        if not out.exists():
            result.append(sp)
    return result
