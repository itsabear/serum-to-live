#!/usr/bin/env python3
"""
Serum 2.0.x .SerumPreset → Ableton .vstpreset (Serum 2.1.4) converter.
All conversion logic lives here; the UI imports convert_one and convert_folder.
"""

import sys, struct, json, hashlib, copy, re
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

# ── public API ────────────────────────────────────────────────────────────────

def convert_one(sp_path: Path, out_path: Path) -> None:
    """Convert a single .SerumPreset to .vstpreset. Raises on error."""
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


def output_path_for(sp_path: Path, input_root: Path, output_root: Path, flat: bool) -> Path:
    if flat:
        return output_root / (sp_path.stem + ".vstpreset")
    rel = sp_path.relative_to(input_root)
    return output_root / rel.with_suffix(".vstpreset")


def find_presets(input_root: Path) -> list[Path]:
    return sorted(input_root.rglob("*.SerumPreset"))


def find_new_presets(input_root: Path, output_root: Path, flat: bool) -> list[Path]:
    """Return only presets that don't already have a matching .vstpreset in output."""
    result = []
    for sp in find_presets(input_root):
        out = output_path_for(sp, input_root, output_root, flat)
        if not out.exists():
            result.append(sp)
    return result
