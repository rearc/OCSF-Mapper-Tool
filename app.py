"""OCSF Mapper — Databricks App (v4).

Four-tab tool: Generator, Sample Inspector, OCSF Explorer, Library.

v4 changes:
  - Removed the "Submit for review" / PR-staging flow. The app now generates,
    validates, and lets the user Save to Volume or Download. Promotion to the
    repo (PR creation) is handled outside this app.
  - OCSF version default corrected to 1.7.0 (1.8.0 does not exist).
"""
from __future__ import annotations

import gzip
import html
import io
import json
import os
import re
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import streamlit as st

from ocsf_mapper import run
from ocsf_mapper.fetch_ocsf import fetch_classes_list, fetch_class
from ocsf_mapper.profiler import detect_format, profile, render_profile_for_llm
from ocsf_mapper.reference_library import parse_preset_metadata


# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_REFERENCE_DIR = "/Volumes/dsl_dev/internal/ocsf_mapper/preset_library"
DEFAULT_OUTPUT_DIR = "/Volumes/dsl_dev/internal/ocsf_mapper/generated_presets"
DEFAULT_CACHE_DIR = "/Volumes/dsl_dev/internal/ocsf_mapper/schema_cache"
DEFAULT_OCSF_VERSION = "1.7.0"

# ─── Volume access (Databricks SDK) ──────────────────────────────────────────

def _in_app() -> bool:
    return os.environ.get("DATABRICKS_APP_NAME") is not None and not Path("/Volumes").exists()


def _sdk():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


def volume_exists(path: str) -> bool:
    if not _in_app():
        return Path(path).exists()
    try:
        w = _sdk()
        try:
            list(w.files.list_directory_contents(path))
            return True
        except Exception:
            pass
        try:
            w.files.get_metadata(path)
            return True
        except Exception:
            return False
    except Exception:
        return False


def volume_list_yaml(directory: str) -> list[str]:
    if not _in_app():
        p = Path(directory)
        if not p.is_dir():
            return []
        return sorted([str(x) for x in p.glob("*.yaml")] + [str(x) for x in p.glob("*.yml")])
    try:
        w = _sdk()
        entries = list(w.files.list_directory_contents(directory))
        return sorted([e.path for e in entries if e.path and (e.path.endswith(".yaml") or e.path.endswith(".yml"))])
    except Exception:
        return []


def volume_read_bytes(path: str) -> bytes:
    if not _in_app():
        return Path(path).read_bytes()
    w = _sdk()
    resp = w.files.download(path)
    return resp.contents.read()


def volume_read_text(path: str) -> str:
    return volume_read_bytes(path).decode("utf-8", errors="replace")


def volume_download_to_local(volume_path: str) -> str:
    if not _in_app():
        return volume_path
    data = volume_read_bytes(volume_path)
    local = Path(tempfile.mkdtemp(prefix="ocsf_")) / Path(volume_path).name
    local.write_bytes(data)
    return str(local)


def volume_download_dir(volume_dir: str) -> str:
    if not _in_app():
        return volume_dir
    local = Path(tempfile.mkdtemp(prefix="ocsf_ref_"))
    for p in volume_list_yaml(volume_dir):
        try:
            (local / Path(p).name).write_bytes(volume_read_bytes(p))
        except Exception:
            continue
    return str(local)


def volume_write_text(path: str, content: str) -> None:
    if not _in_app():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)
        return
    w = _sdk()
    w.files.upload(path, io.BytesIO(content.encode("utf-8")), overwrite=True)


def volume_delete(path: str) -> None:
    if not _in_app():
        Path(path).unlink(missing_ok=True)
        return
    w = _sdk()
    w.files.delete(path)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _safe_filename(vendor: str, source_type: str) -> str:
    base = f"{vendor}_{source_type}"
    base = re.sub(r"[^a-zA-Z0-9_\-]", "_", base).strip("_").lower()
    return base or "preset"


def _html_escape(text: str) -> str:
    """Escape HTML for safe rendering inside markdown(unsafe_allow_html=True)."""
    return html.escape(text)


def _parse_first_records(data: bytes, n: int = 3) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped.startswith("{"):
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
                if len(records) >= n:
                    return records
            except json.JSONDecodeError:
                break
        if records:
            return records
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v[:n]
                return [obj]
        except json.JSONDecodeError:
            pass
    if stripped.startswith("["):
        try:
            arr = json.loads(text)
            return arr[:n]
        except json.JSONDecodeError:
            pass
    return []


def _init_state():
    defaults = {
        "result": None, "error": None,
        "preset_text": "", "report_text": "",
        "save_message": None,
        "prefill_sample_path": "", "prefill_class_uids": "",
        "prefill_inspect_path": "",
        "inspect_pending": False,
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "ocsf_version": DEFAULT_OCSF_VERSION,
        "reference_dir": DEFAULT_REFERENCE_DIR,
        "output_dir": DEFAULT_OUTPUT_DIR,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


# ─── CSS ─────────────────────────────────────────────────────────────────────

CUSTOM_CSS = """
<style>
  /* Hide Streamlit chrome */
  #MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }

  /* Container — slightly wider for breathing room */
  .block-container { padding-top: 1rem !important; padding-bottom: 2rem !important; max-width: 1500px; }

  /* App header */
  .app-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 24px; margin: -1rem -1rem 20px -1rem;
    background: #1A2850; border-bottom: 1px solid #2D3E6B;
  }
  .app-header .title { display: flex; align-items: center; gap: 14px; }
  .app-header .title .icon { font-size: 26px; }
  .app-header .title .name { font-size: 22px; font-weight: 600; color: #FFFFFF; letter-spacing: -0.01em; }
  .app-header .title .sub { font-size: 14px; color: #94A3B8; margin-top: 3px; }
  .app-header .meta { display: flex; align-items: center; gap: 12px; font-size: 14px; color: #94A3B8; }
  .app-header .brand { color: #F5BE2D; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; font-size: 12px; }
  .app-header .brand::before { content: "●"; margin-right: 6px; }

  /* Stat cards */
  .stat-grid { display: flex; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }
  .stat-card {
    background: #1A2850; border: 1px solid #2D3E6B; border-radius: 8px;
    padding: 14px 20px; min-width: 130px;
  }
  .stat-card .num { font-size: 28px; font-weight: 700; line-height: 1.1; color: #FFFFFF; }
  .stat-card .label { font-size: 13px; color: #94A3B8; margin-top: 5px; letter-spacing: 0.02em; }
  .stat-card.primary .num { color: #60A5FA; }
  .stat-card.success .num { color: #22C55E; }
  .stat-card.warn .num { color: #F5BE2D; }
  .stat-card.danger .num { color: #EF4444; }
  .stat-card.orange .num { color: #EA580C; }

  /* Info banner */
  .info-banner {
    background: #14234A; border: 1px solid #2563EB;
    padding: 14px 18px; border-radius: 8px; margin-bottom: 16px; font-size: 14px;
    color: #CBD5E1; line-height: 1.5;
  }
  .info-banner code {
    background: #0A1834; padding: 2px 6px; border-radius: 4px;
    font-size: 13px; color: #93C5FD;
  }


  /* Tabs */
  .stTabs [data-baseweb="tab-list"] {
    gap: 0; background: transparent; padding: 0;
    border-bottom: 1px solid #2D3E6B; margin-bottom: 18px;
  }
  .stTabs [data-baseweb="tab"] {
    background: transparent; color: #94A3B8; padding: 12px 20px;
    font-size: 15px; font-weight: 500; border-radius: 0;
    border-bottom: 2px solid transparent;
  }
  .stTabs [aria-selected="true"] {
    color: #FFFFFF !important;
    border-bottom: 2px solid #2563EB !important;
    background: transparent !important;
  }

  /* Buttons */
  .stButton > button {
    background: #2563EB; color: #FFFFFF; border: none; font-weight: 500; font-size: 14px;
    padding: 10px 18px; border-radius: 6px;
  }
  .stButton > button:hover { background: #1D4ED8; }
  .stButton > button[kind="secondary"] { background: transparent; border: 1px solid #2D3E6B; color: #CBD5E1; }
  .stButton > button[kind="secondary"]:hover { border-color: #94A3B8; color: #FFFFFF; background: transparent; }
  .stDownloadButton > button { background: transparent; border: 1px solid #2D3E6B; color: #CBD5E1; font-size: 14px; }
  .stDownloadButton > button:hover { border-color: #94A3B8; color: #FFFFFF; }

  /* Inputs — sit on a slightly raised surface */
  .stTextInput input, .stTextArea textarea, .stSelectbox > div > div, .stNumberInput input {
    background: #1A2850 !important; border-color: #2D3E6B !important; color: #E2E8F0 !important;
    font-size: 14px;
  }
  .stTextInput label, .stTextArea label, .stSelectbox label, .stNumberInput label {
    font-size: 13px !important; color: #94A3B8 !important;
  }

  /* Code blocks */
  .stCodeBlock, pre, code { background: #1A2850 !important; border: 1px solid #2D3E6B !important; border-radius: 6px; font-size: 13px !important; }
  .stCodeBlock > div { background: #1A2850 !important; }

  /* Sidebar */
  section[data-testid="stSidebar"] { background: #14234A; border-right: 1px solid #2D3E6B; }
  section[data-testid="stSidebar"] h2 { color: #FFFFFF; font-size: 17px; }

  /* Dataframes */
  .stDataFrame { border: 1px solid #2D3E6B; border-radius: 8px; overflow: hidden; font-size: 14px; }

  /* Status pills */
  .status-pill { padding: 10px 14px; border-radius: 6px; font-size: 14px; margin-bottom: 8px; }
  .status-pill.ok { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.4); color: #86EFAC; }
  .status-pill.warn { background: rgba(245, 190, 45, 0.15); border: 1px solid rgba(245, 190, 45, 0.4); color: #FDE68A; }
  .status-pill.err { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #FCA5A5; }

  /* Captions — bumped from 12px to 13px */
  .stCaption, [data-testid="stCaptionContainer"] { color: #94A3B8 !important; font-size: 13px !important; }

  /* H4 (section headings inside tabs) */
  h4 { color: #FFFFFF; font-weight: 600; font-size: 18px; margin-bottom: 6px !important; }

  /* Body text — bump default */
  .stMarkdown p { font-size: 14px; line-height: 1.55; }

  /* Divider */
  hr { border-color: #2D3E6B !important; }

  /* Ace editor frame — match the slate/navy palette */
  .ace_editor {
    border: 1px solid #2D3E6B !important;
    border-radius: 8px !important;
  }
  .ace-tomorrow-night-blue {
    background: #1A2850 !important;
  }
  .ace_gutter {
    background: #14234A !important;
    border-right: 1px solid #2D3E6B !important;
  }
</style>
"""

def render_header():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="app-header">
          <div class="title">
            <span class="icon">🧭</span>
            <div>
              <div class="name">OCSF Mapper</div>
              <div class="sub">Vendor samples → OCSF → Lakewatch presets. All in one place.</div>
            </div>
          </div>
          <div class="meta">
            <span style="color: #22C55E;">●</span>
            <span>Connected</span>
            <span style="color: #475569;">|</span>
            <span class="brand">by Rearc</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def stat_cards(cards: list[dict]):
    """Render a grid of stat cards. Each card = {num, label, kind?}"""
    html = '<div class="stat-grid">'
    for c in cards:
        kind = c.get("kind", "")
        html += (
            f'<div class="stat-card {kind}">'
            f'  <div class="num">{c["num"]}</div>'
            f'  <div class="label">{c["label"]}</div>'
            f'</div>'
        )
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def status_pill(text: str, kind: str = "ok"):
    st.markdown(f'<div class="status-pill {kind}">{text}</div>', unsafe_allow_html=True)


# ─── Page setup ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="OCSF Mapper", page_icon="🧭", layout="wide")
_init_state()
render_header()

@st.cache_data(show_spinner=False, ttl=3600)
def _ocsf_version_exists(version: str) -> tuple[bool, str]:
    """Check if an OCSF version exists at schema.ocsf.io. Returns (exists, message)."""
    if not version or not version.strip():
        return False, "version is empty"
    import urllib.request
    import urllib.error
    url = f"https://schema.ocsf.io/api/{version.strip()}/classes"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            if 200 <= r.status < 300:
                return True, f"OCSF {version} ✓"
            return False, f"OCSF {version} returned HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, f"OCSF {version} does not exist at schema.ocsf.io"
        return False, f"OCSF {version} returned HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"Could not reach schema.ocsf.io: {e.reason}"
    except Exception as e:
        return False, f"Check failed: {e}"


# Sidebar
with st.sidebar:
    st.header("Configuration")
    st.session_state.api_key = st.text_input("Anthropic API key", value=st.session_state.api_key, type="password")
    _common_versions = ["1.7.0", "1.6.0", "1.5.0", "1.4.0", "1.3.0", "Other..."]
    _current = st.session_state.ocsf_version
    _default_idx = _common_versions.index(_current) if _current in _common_versions else len(_common_versions) - 1
    _picked = st.selectbox("OCSF version", _common_versions, index=_default_idx, key="ocsf_version_picker")
    if _picked == "Other...":
        st.session_state.ocsf_version = st.text_input(
            "Custom version",
            value=_current if _current not in _common_versions else "",
            placeholder="e.g. 1.2.0",
            help="Any version available at schema.ocsf.io",
        )
    else:
        st.session_state.ocsf_version = _picked

    # Check version existence and surface result
    if st.session_state.ocsf_version:
        _exists, _msg = _ocsf_version_exists(st.session_state.ocsf_version)
        if _exists:
            status_pill(_msg, "ok")
        else:
            status_pill(f"⚠ {_msg}", "err")
    st.session_state.reference_dir = st.text_input("Reference library", value=st.session_state.reference_dir)
    st.session_state.output_dir = st.text_input("Output Volume", value=st.session_state.output_dir)
    st.divider()
    if volume_exists(st.session_state.reference_dir):
        yamls = volume_list_yaml(st.session_state.reference_dir)
        if yamls:
            status_pill(f"📚 {len(yamls)} reference(s) in library", "ok")
        else:
            status_pill("📚 Library folder is empty", "warn")
    else:
        status_pill("📚 Library path does not exist", "err")

# ─── Tab 1: Generator ────────────────────────────────────────────────────────

def render_generator_tab():
    st.markdown("#### Generate a preset")
    st.caption("Classify → fetch OCSF schema → generate preset using style references. Edit before saving.")

    # Banner if user just queued an inspection from this tab earlier
    if st.session_state.get("inspect_pending"):
        st.markdown(
            '<div class="info-banner">🔍 Sample queued for inspection — click the '
            '<strong>Sample Inspector</strong> tab above to view it.</div>',
            unsafe_allow_html=True,
        )

    col_inputs, col_output = st.columns([1, 1])
    with col_inputs:
        sample_path = st.text_input(
            "Sample path (Volume)",
            value=st.session_state.get("prefill_sample_path", ""),
            placeholder="/Volumes/.../samples/vendor_sample.jsonl",
            key="gen_sample_path",
        )
        if st.button("🔍 Inspect this sample", type="secondary", use_container_width=True, disabled=not sample_path):
            st.session_state.prefill_inspect_path = sample_path
            st.session_state.inspect_pending = True
            st.rerun()

        vendor = st.text_input("Vendor", placeholder="snyk", key="gen_vendor")
        source_type = st.text_input("Source type", placeholder="vulnerabilities", key="gen_source_type")
        class_override = st.text_input(
            "Class uids override (optional)",
            value=st.session_state.get("prefill_class_uids", ""),
            placeholder="blank = classifier auto-picks; else e.g. 2002,5020",
            key="gen_class_override",
        )
        if st.session_state.get("prefill_sample_path"):
            st.session_state.prefill_sample_path = ""
        if st.session_state.get("prefill_class_uids"):
            st.session_state.prefill_class_uids = ""
        generate_button = st.button("Generate preset", type="primary", use_container_width=True)

    if generate_button:
        st.session_state.result = None
        st.session_state.error = None
        st.session_state.save_message = None
        if not st.session_state.api_key.strip():
            st.session_state.error = "Paste your Anthropic API key in the sidebar."
        elif not (sample_path and vendor and source_type):
            st.session_state.error = "Fill sample path, vendor, and source type."
        elif not volume_exists(sample_path):
            st.session_state.error = f"Sample not found: {sample_path}"
        else:
            with col_output:
                st.markdown("**Live progress**")
                phase_classify = st.empty()
                phase_fetch = st.empty()
                phase_generate = st.empty()
                generate_stream_box = st.empty()

            phase_classify.markdown(
                '<div class="info-banner">⏳ Preparing — downloading sample and references...</div>',
                unsafe_allow_html=True,
            )

            try:
                os.environ["ANTHROPIC_API_KEY"] = st.session_state.api_key.strip()
                class_uids = None
                if class_override.strip():
                    class_uids = [int(x.strip()) for x in class_override.split(",") if x.strip()]

                local_sample = volume_download_to_local(sample_path)
                local_refs = volume_download_dir(st.session_state.reference_dir)
                local_cache = "/tmp/ocsf_mapper_cache" if _in_app() else DEFAULT_CACHE_DIR

                stream_state = {"tokens": [], "since_flush": 0}

                def render_phase(placeholder, label, status, message=""):
                    icon = {"pending": "○", "running": "⏳", "done": "✓", "skipped": "—"}[status]
                    color = {"pending": "#94A3B8", "running": "#F5BE2D", "done": "#22C55E", "skipped": "#94A3B8"}[status]
                    placeholder.markdown(
                        f'<div class="info-banner" style="border-color:{color}">'
                        f'<span style="color:{color};font-weight:600">{icon} {label}</span>'
                        f'{("<br/>" + message) if message else ""}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                def cb(phase: str, message: str):
                    if phase == "classify_start":
                        render_phase(phase_classify, "Classifying", "running", message)
                    elif phase == "classify_done":
                        render_phase(phase_classify, "Classify complete", "done", message)
                    elif phase == "fetch_start":
                        render_phase(phase_fetch, "Fetching OCSF schema", "running", message)
                    elif phase == "fetch_done":
                        render_phase(phase_fetch, "Schema ready", "done", message)
                    elif phase == "generate_start":
                        render_phase(phase_generate, "Generating preset", "running", message)
                    elif phase == "generate_token":
                        stream_state["tokens"].append(message)
                        stream_state["since_flush"] += 1
                        if stream_state["since_flush"] >= 30 or "\n" in message:
                            stream_state["since_flush"] = 0
                            text = "".join(stream_state["tokens"])
                            display = text[-2000:] if len(text) > 2000 else text
                            generate_stream_box.markdown(
                                f'<div style="max-height:300px;overflow-y:auto;background:#1A2850;'
                                f'border:1px solid #2D3E6B;border-radius:6px;padding:12px;'
                                f'font-family:monospace;font-size:12px;white-space:pre-wrap;'
                                f'color:#CBD5E1;line-height:1.4;">'
                                f'{_html_escape(display)}'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                    elif phase == "generate_done":
                        render_phase(phase_generate, "Generation complete", "done", message)
                        generate_stream_box.empty()

                render_phase(phase_fetch, "Fetch OCSF schema", "pending")
                render_phase(phase_generate, "Generate preset", "pending")

                result = run(
                    sample_path=local_sample,
                    vendor=vendor,
                    source_type=source_type,
                    ocsf_version=st.session_state.ocsf_version,
                    class_uids=class_uids,
                    reference_dir=local_refs,
                    cache_dir=local_cache,
                    out_dir="/tmp/ocsf_mapper_output",
                    verbose=False,
                    progress_callback=cb,
                )

                st.session_state.result = result
                st.session_state.preset_text = Path(result["preset_path"]).read_text()
                st.session_state.report_text = Path(result["report_path"]).read_text()
                generate_stream_box.empty()
                phase_classify.empty()
                phase_fetch.empty()
                phase_generate.empty()
            except Exception as e:
                st.session_state.error = f"{e}\n\n{traceback.format_exc()}"

    with col_output:
        if st.session_state.error:
            st.error(st.session_state.error)
        elif st.session_state.result:
            r = st.session_state.result
            stat_cards([
                {"num": ", ".join(str(c["uid"]) for c in r["classes"]), "label": "Classes", "kind": "primary"},
                {"num": len(r["references_used"]), "label": "References used", "kind": "success"},
                {"num": r["usage"]["input_tokens"], "label": "Input tokens"},
                {"num": r["usage"]["output_tokens"], "label": "Output tokens"},
            ])
        else:
            st.info("Fill the form and click Generate.")

    if st.session_state.result:
        st.divider()

        # Surface OCSF data-type validation findings prominently.
        type_findings = st.session_state.result.get("type_findings", [])
        if type_findings:
            errors = [f for f in type_findings if f["level"] == "error"]
            warnings = [f for f in type_findings if f["level"] == "warning"]
            kind = "err" if errors else "warn"
            status_pill(
                f"⚠ OCSF type check: {len(errors)} error(s), {len(warnings)} warning(s) — "
                f"see the Generation report tab and fix before saving.",
                kind,
            )
        else:
            status_pill("✓ OCSF type check passed — no datatype violations.", "ok")

        tab_preset, tab_report = st.tabs(["📝 Preset (editable)", "📋 Generation report"])
        with tab_preset:
            from streamlit_ace import st_ace

            edited = st_ace(
                value=st.session_state.preset_text,
                language="yaml",
                theme="tomorrow_night_blue",   # matches your dark slate/navy palette
                height=700,
                font_size=15,
                tab_size=2,
                wrap=True,
                show_gutter=True,               # line numbers
                show_print_margin=False,
                auto_update=True,               # commit changes on every keystroke
                key="preset_editor_ace",
            )
            st.session_state.preset_text = edited

            col_save, col_dl = st.columns(2)

            with col_save:
                if st.button("💾 Save preset to Volume", use_container_width=True, type="primary"):
                    try:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        base = _safe_filename(vendor or "preset", source_type or "gen")
                        preset_dest = f"{st.session_state.output_dir}/{base}_{ts}_preset.yaml"
                        report_dest = f"{st.session_state.output_dir}/{base}_{ts}_report.md"
                        volume_write_text(preset_dest, edited)
                        volume_write_text(report_dest, st.session_state.report_text)
                        st.session_state.save_message = f"✓ Saved\n- `{preset_dest}`\n- `{report_dest}`"
                    except Exception as e:
                        st.session_state.save_message = f"Save failed: {e}"

            with col_dl:
                fname = f"{_safe_filename(vendor or 'preset', source_type or 'gen')}_preset.yaml"
                st.download_button(
                    "⬇️ Download preset",
                    data=edited,
                    file_name=fname,
                    mime="application/yaml",
                    use_container_width=True,
                )

            if st.session_state.save_message:
                if st.session_state.save_message.startswith("✓"):
                    st.success(st.session_state.save_message)
                else:
                    st.error(st.session_state.save_message)
        with tab_report:
            st.markdown(st.session_state.report_text)

# ─── Tab 2: Sample Inspector ─────────────────────────────────────────────────

def render_inspector_tab():
    st.markdown("#### Inspect a sample")
    st.caption("Peek at JSON structure and profile of a vendor sample before generating.")

    # Consume any prefill from Generator tab
    initial = st.session_state.get("prefill_inspect_path", "")
    if initial:
        st.session_state.prefill_inspect_path = ""
        st.session_state.inspect_pending = False

    sample_path = st.text_input(
        "Sample path (Volume)",
        value=initial,
        placeholder="/Volumes/.../samples/vendor_sample.jsonl",
        key="inspect_sample_path",
    )
    inspect_btn = st.button("Inspect", type="primary")

    if inspect_btn:
        if not sample_path.strip():
            st.warning("Paste a sample path first.")
            return
        if not volume_exists(sample_path):
            st.error(f"Sample not found: {sample_path}")
            return
        try:
            with st.spinner("Reading sample..."):
                local_path = volume_download_to_local(sample_path)
            fmt = detect_format(local_path)
            prof = profile(local_path, fmt, max_records=100)
            data = Path(local_path).read_bytes()
            if sample_path.endswith(".gz"):
                data = gzip.decompress(data)
            records = _parse_first_records(data, n=3)

            stat_cards([
                {"num": fmt["format"], "label": "Format", "kind": "primary"},
                {"num": prof["records_profiled"], "label": "Records profiled"},
                {"num": prof["field_count"], "label": "Distinct fields"},
                {"num": len(records), "label": "Previewed"},
            ])
            if fmt.get("record_path"):
                st.markdown(
                    f'<div class="info-banner">Record path: <code>{fmt["record_path"]}</code> — {fmt["notes"]}</div>',
                    unsafe_allow_html=True,
                )

            sub_tab_json, sub_tab_profile = st.tabs(["📄 First records", "📊 Profile"])
            with sub_tab_json:
                if records:
                    for i, rec in enumerate(records, 1):
                        with st.expander(f"Record {i}", expanded=(i == 1)):
                            st.json(rec, expanded=False)
                else:
                    st.warning("Couldn't parse any records for preview.")
            with sub_tab_profile:
                st.code(render_profile_for_llm(prof, max_fields=200), language="text")

            st.divider()
            if st.button("→ Use this sample in Generator"):
                st.session_state.prefill_sample_path = sample_path
                st.info("Sample path pre-filled in the Generator tab.")
        except Exception as e:
            st.error(f"Inspection failed: {e}")
            st.code(traceback.format_exc())


# ─── Tab 3: OCSF Explorer ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _cached_classes_list(version: str) -> list[dict]:
    catalog = fetch_classes_list(version)
    out = []
    for name, meta in catalog.items():
        if not isinstance(meta, dict):
            continue
        uid = meta.get("uid")
        if uid is None:
            continue
        out.append({
            "uid": uid, "name": name,
            "caption": meta.get("caption", name),
            "category": meta.get("category_name", ""),
        })
    return sorted(out, key=lambda c: c["uid"])


@st.cache_data(show_spinner=False)
def _cached_class_detail(version: str, class_name: str) -> dict:
    return fetch_class(version, class_name) or {}


def render_explorer_tab():
    st.markdown("#### OCSF Explorer")
    st.caption(f"Browse all classes in OCSF {st.session_state.ocsf_version}.")

    try:
        with st.spinner("Loading class catalog..."):
            classes = _cached_classes_list(st.session_state.ocsf_version)
    except Exception as e:
        st.error(f"Failed to load OCSF catalog: {e}")
        return

    # Overall stats
    categories = {c["category"] for c in classes if c["category"]}
    stat_cards([
        {"num": len(classes), "label": "Total classes", "kind": "primary"},
        {"num": len(categories), "label": "Categories"},
        {"num": st.session_state.ocsf_version, "label": "Version"},
    ])

    query = st.text_input("Filter", placeholder="Search by name, caption, uid, or category", key="explorer_filter")
    q = query.lower().strip()
    filtered = [
        c for c in classes
        if not q or q in str(c["uid"]) or q in c["name"].lower()
        or q in c["caption"].lower() or q in c["category"].lower()
    ]
    st.caption(f"{len(filtered)} of {len(classes)} classes shown")

    options = [f"{c['uid']:>6} — {c['caption']}  ·  {c['category']}" for c in filtered]
    picks = st.multiselect(
        "Classes (select one or more)",
        options,
        key="explorer_picks",
        help="Pick multiple to send them all to the Generator at once.",
    )

    # Bulk action — push all selected to Generator
    if picks:
        selected_classes = [filtered[options.index(p)] for p in picks]
        uids = [c["uid"] for c in selected_classes]
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.markdown(
                f'<div class="info-banner">Selected <strong>{len(uids)}</strong> class(es): '
                f'{", ".join(str(u) for u in uids)}</div>',
                unsafe_allow_html=True,
            )
        with col_b:
            if st.button(f"→ Use {len(uids)} in Generator", type="primary", use_container_width=True):
                st.session_state.prefill_class_uids = ",".join(str(u) for u in uids)
                st.success(f"{len(uids)} class(es) pre-filled in Generator.")

        st.divider()
        st.caption("Detail view (showing first selected class — others will still be sent):")
        selected = selected_classes[0]
        try:
            with st.spinner(f"Loading class {selected['uid']}..."):
                detail = _cached_class_detail(st.session_state.ocsf_version, selected["name"])
        except Exception as e:
            st.error(f"Failed to load class detail: {e}")
            return

        attrs = detail.get("attributes", {})
        rows = []
        # OCSF API attribute shape varies by version:
        #  - 1.6+: dict[attr_name -> dict(type, requirement, ...)]
        #  - some 1.5 / earlier: list[dict(name=..., type=..., ...)]
        #  - some older versions wrap differently
        if isinstance(attrs, dict):
            # Standard shape — dict of name → meta
            for name, a in attrs.items():
                if isinstance(a, dict):
                    rows.append({
                        "attribute": name,
                        "type": a.get("type") or a.get("object_type") or a.get("type_name") or "—",
                        "requirement": a.get("requirement", ""),
                        "is_array": bool(a.get("is_array", False)),
                        "has_enum": "enum" in a,
                    })
                elif isinstance(a, str):
                    # Some older versions have name → type-string shorthand
                    rows.append({
                        "attribute": name, "type": a, "requirement": "",
                        "is_array": False, "has_enum": False,
                    })
        elif isinstance(attrs, list):
            for i, a in enumerate(attrs):
                if not isinstance(a, dict):
                    continue
                # Try common name keys in priority order
                name = a.get("name") or a.get("caption") or a.get("attribute") or f"attr_{i}"
                rows.append({
                    "attribute": name,
                    "type": a.get("type") or a.get("object_type") or a.get("type_name") or "—",
                    "requirement": a.get("requirement", ""),
                    "is_array": bool(a.get("is_array", False)),
                    "has_enum": "enum" in a,
                })

        # Diagnostic — if we couldn't extract anything useful, show raw shape
        if rows and all(r["type"] == "—" and not r["requirement"] for r in rows):
            with st.expander("⚠ Couldn't parse attribute details — show raw response"):
                st.caption(
                    "OCSF returned a shape this version of the app doesn't recognize. "
                    "First 3 raw entries below — share these so we can fix the parser."
                )
                if isinstance(attrs, dict):
                    sample = dict(list(attrs.items())[:3])
                else:
                    sample = attrs[:3] if isinstance(attrs, list) else attrs
                st.json(sample)
        required = sum(1 for r in rows if r["requirement"] == "required")
        recommended = sum(1 for r in rows if r["requirement"] == "recommended")
        optional = sum(1 for r in rows if r["requirement"] == "optional")

        stat_cards([
            {"num": selected["uid"], "label": "class_uid", "kind": "primary"},
            {"num": len(rows), "label": "Attributes"},
            {"num": required, "label": "Required", "kind": "danger"},
            {"num": recommended, "label": "Recommended", "kind": "warn"},
            {"num": optional, "label": "Optional", "kind": "success"},
        ])

        if detail.get("description"):
            st.markdown(
                f'<div class="info-banner">{detail["description"]}</div>',
                unsafe_allow_html=True,
            )

        rows.sort(key=lambda r: (
            {"required": 0, "recommended": 1, "optional": 2}.get(r["requirement"], 3),
            r["attribute"],
        ))
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True, height=400)


# ─── Tab 4: Library ──────────────────────────────────────────────────────────

def render_library_tab():
    st.markdown("#### Preset library")
    st.caption(f"References at `{st.session_state.reference_dir}`. Used as style anchors during generation.")

    if not volume_exists(st.session_state.reference_dir):
        st.error("Library path does not exist. Set it in the sidebar.")
        return

    yamls = volume_list_yaml(st.session_state.reference_dir)
    if not yamls:
        st.warning("Library is empty. Upload existing preset YAMLs to guide generation.")
        return

    entries = []
    all_classes = set()
    all_categories = set()
    total_size = 0
    for path in yamls:
        try:
            local = volume_download_to_local(path)
            meta = parse_preset_metadata(Path(local))
            entries.append({
                "path": path, "name": Path(path).name,
                "class_uids": meta["class_uids"],
                "category_uids": meta["category_uids"],
                "size_chars": meta["size_chars"],
            })
            all_classes.update(meta["class_uids"])
            all_categories.update(meta["category_uids"])
            total_size += meta["size_chars"]
        except Exception as e:
            entries.append({
                "path": path, "name": Path(path).name, "class_uids": [], "category_uids": [],
                "size_chars": 0, "error": str(e),
            })

    stat_cards([
        {"num": len(entries), "label": "Presets", "kind": "primary"},
        {"num": len(all_classes), "label": "OCSF classes covered"},
        {"num": len(all_categories), "label": "Categories"},
        {"num": f"{total_size // 1024}K", "label": "Total size"},
    ])

    st.dataframe(
        [{
            "name": e["name"],
            "class_uids": ", ".join(str(u) for u in e["class_uids"]) or "—",
            "categories": ", ".join(str(u) for u in e["category_uids"]) or "—",
            "size (chars)": f"{e['size_chars']:,}",
        } for e in entries],
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    names = [e["name"] for e in entries]
    pick = st.selectbox("View / manage a preset", names, key="library_pick")
    if pick:
        entry = next(e for e in entries if e["name"] == pick)
        try:
            content = volume_read_text(entry["path"])
        except Exception as e:
            st.error(f"Failed to read: {e}")
            return

        tab_view, tab_actions = st.tabs(["📄 View", "🛠 Actions"])
        with tab_view:
            st.code(content[:50_000], language="yaml")
            if len(content) > 50_000:
                st.caption(f"(truncated — full size {len(content):,} chars)")
        with tab_actions:
            st.download_button("⬇️ Download", data=content, file_name=entry["name"], mime="application/yaml")
            st.markdown("---")
            st.warning("Deleting removes this preset from the library.")
            confirm = st.text_input(f"Type `{entry['name']}` to confirm deletion", key=f"confirm_del_{entry['name']}")
            if st.button("🗑 Delete", disabled=(confirm != entry["name"])):
                try:
                    volume_delete(entry["path"])
                    st.success(f"Deleted {entry['name']}. Refresh to see updated list.")
                except Exception as e:
                    st.error(f"Delete failed: {e}")


# ─── Main tabs ───────────────────────────────────────────────────────────────

tab_gen, tab_inspect, tab_explore, tab_library = st.tabs([
    "🧭  Generator",
    "🔍  Sample Inspector",
    "📚  OCSF Explorer",
    "📂  Library",
])

with tab_gen:
    render_generator_tab()
with tab_inspect:
    render_inspector_tab()
with tab_explore:
    render_explorer_tab()
with tab_library:
    render_library_tab()
