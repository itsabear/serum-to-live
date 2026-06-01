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

# Serum 1 parameter index → (Serum 2 CBOR section, kParam key)
# Section path uses '.' for nesting: "Oscillator0.WTOsc0" means
# obj["Oscillator0"]["WTOsc0"]["plainParams"][key]
_S1_PARAM_MAP = {
    # Global
    0:  ("Global0",             "kParamMasterVolume"),
    # Oscillator A → Oscillator0
    1:  ("Oscillator0",         "kParamVolume"),
    2:  ("Oscillator0",         "kParamPan"),
    3:  ("Oscillator0",         "kParamOctave"),
    4:  ("Oscillator0",         "kParamTranspose"),
    5:  ("Oscillator0",         "kParamFine"),
    6:  ("Oscillator0",         "kParamUnison"),
    7:  ("Oscillator0",         "kParamDetune"),
    8:  ("Oscillator0",         "kParamDetuneWid"),
    9:  ("Oscillator0.WTOsc0",  "kParamWarp"),
    10: ("Oscillator0",         "kParamCoarsePit"),
    11: ("Oscillator0.WTOsc0",  "kParamTablePos"),
    12: ("Oscillator0.WTOsc0",  "kParamRandomPhase"),
    13: ("Oscillator0.WTOsc0",  "kParamInitialPhase"),
    # Oscillator B → Oscillator1
    14: ("Oscillator1",         "kParamVolume"),
    15: ("Oscillator1",         "kParamPan"),
    16: ("Oscillator1",         "kParamOctave"),
    17: ("Oscillator1",         "kParamTranspose"),
    18: ("Oscillator1",         "kParamFine"),
    19: ("Oscillator1",         "kParamUnison"),
    20: ("Oscillator1",         "kParamDetune"),
    21: ("Oscillator1",         "kParamDetuneWid"),
    22: ("Oscillator1.WTOsc0",  "kParamWarp"),
    23: ("Oscillator1",         "kParamCoarsePit"),
    24: ("Oscillator1.WTOsc0",  "kParamTablePos"),
    25: ("Oscillator1.WTOsc0",  "kParamRandomPhase"),
    26: ("Oscillator1.WTOsc0",  "kParamInitialPhase"),
    # Noise → Oscillator2
    27: ("Oscillator2",         "kParamVolume"),
    28: ("Oscillator2",         "kParamPitch"),
    29: ("Oscillator2",         "kParamFine"),
    30: ("Oscillator2",         "kParamPan"),
    # Amplitude envelope (Serum 1 Env1 → Serum 2 Env0)
    35: ("Env0",                "kParamAttack"),
    36: ("Env0",                "kParamHold"),
    37: ("Env0",                "kParamDecay"),
    38: ("Env0",                "kParamSustain"),
    39: ("Env0",                "kParamRelease"),
    # Filter
    45: ("VoiceFilter0",        "kParamFreq"),
    46: ("VoiceFilter0",        "kParamReso"),
    47: ("VoiceFilter0",        "kParamDrive"),
    48: ("VoiceFilter0",        "kParamVar"),
    49: ("VoiceFilter0",        "kParamWet"),
    # Envelope 2 (Serum 1 Env2 → Serum 2 Env1)
    51: ("Env1",                "kParamAttack"),
    52: ("Env1",                "kParamHold"),
    53: ("Env1",                "kParamDecay"),
    54: ("Env1",                "kParamSustain"),
    55: ("Env1",                "kParamRelease"),
    # Envelope 3 (Serum 1 Env3 → Serum 2 Env2)
    56: ("Env2",                "kParamAttack"),
    57: ("Env2",                "kParamHold"),
    58: ("Env2",                "kParamDecay"),
    59: ("Env2",                "kParamSustain"),
    60: ("Env2",                "kParamRelease"),
    # LFO rates
    61: ("LFO0",                "kParamRate"),
    62: ("LFO1",                "kParamRate"),
    63: ("LFO2",                "kParamRate"),
    64: ("LFO3",                "kParamRate"),
    # Portamento
    65: ("VoicePanel0",         "kParamPortamentoTime"),
    66: ("VoicePanel0",         "kParamPortamentoCurve"),
    # Oscillator enable flags
    212: ("Oscillator0",        "kParamEnable"),
    213: ("Oscillator1",        "kParamEnable"),
    216: ("VoiceFilter0",       "kParamEnable"),
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
    Returns (preset_name, author, params_list, wt_a, wt_b, noise).
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

    # Try preset name from raw bytes (more reliable than FXP header)
    raw_name = _read_s1_str(raw, 0x4972, 32)
    if raw_name:
        preset_name = raw_name

    return preset_name, "", params, wt_a, wt_b, noise

def _build_s1_cbor(params: list, wt_a: str, wt_b: str, noise: str, obj_init: dict) -> dict:
    """Build a Serum 2 processor CBOR from Serum 1 FXP data."""
    result = copy.deepcopy(obj_init)

    # Apply mapped parameters
    for idx, (section_path, kparam) in _S1_PARAM_MAP.items():
        if idx >= len(params):
            continue
        parts = section_path.split(".")
        node = result
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
        pp = node.setdefault("plainParams", {})
        if isinstance(pp, dict):
            pp[kparam] = float(params[idx])

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
    preset_name, author, params, wt_a, wt_b, noise = _parse_fxp(fxp_path)

    obj_214 = _build_s1_cbor(params, wt_a, wt_b, noise, t.obj_init)
    comp    = _build_xfer(t.comp_meta, obj_214)

    cont_obj = copy.deepcopy(t.obj_cont)
    cont_obj["presetName"]          = preset_name
    cont_obj["presetAuthor"]        = author
    cont_obj["presetDescription"]   = ""
    cont_obj["selectedPresetPath"]  = str(fxp_path)
    cont_obj["presetHasBeenEdited"] = False

    cont_meta = dict(t.cont_meta)
    cont_meta["presetName"]        = preset_name
    cont_meta["presetAuthor"]      = author
    cont_meta["presetDescription"] = ""
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
