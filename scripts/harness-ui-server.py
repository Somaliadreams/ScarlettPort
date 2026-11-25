#!/usr/bin/env python3
"""
ScarlettPort Harness UI Server & Agent Loop Control Plane.

Zero-dependency Python 3 server wrapping scripts/harness.ps1 and logs/harness/*:
- Serves the live web dashboard (scripts/harness-ui.html)
- Parses victory run logs into both aggregate metrics and per-sample time series
- Launches/stops/monitors harness.ps1 runs and streams live status via SSE
- Exposes a structured JSON REST API + loop/experiment ledger for autonomous agent loops
"""

import argparse
import collections
import datetime
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
LOGS_ROOT = ROOT_DIR / "logs"
HARNESS_LOGS_DIR = LOGS_ROOT / "harness"
UI_HTML_PATH = SCRIPTS_DIR / "harness-ui.html"
MATRIX_PATH = SCRIPTS_DIR / "harness-matrix.json"
HARNESS_PS1 = SCRIPTS_DIR / "harness.ps1"
UI_CONFIG_PATH = HARNESS_LOGS_DIR / "ui-config.json"
LOOP_STATE_PATH = HARNESS_LOGS_DIR / "loop.json"
EXPERIMENTS_PATH = HARNESS_LOGS_DIR / "experiments.jsonl"
TMP_CUSTOM_MATRIX_DIR = HARNESS_LOGS_DIR / "_custom_matrices"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RX_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)")
RX_FAST_BOOT = re.compile(r"fast boot: in world after (\d+) frames, ([0-9.]+) s \(([^)]+)\)")
RX_REBAR = re.compile(r"Resizable BAR \(D3D12 GPU upload heaps\):\s*(.+)$")
RX_GPU_HEAP = re.compile(r"gpu_upload_heaps=(true|false)")
RX_BUDGET = re.compile(r"card_budget_mb=(?:Some\()?(\d+)\)?")
RX_FRAME = re.compile(
    r"([0-9.]+) fps, ([0-9.]+) ms/frame "
    r"\(([0-9.]+) preparing: copies ([0-9.]+) ms ([0-9.]+) MB, "
    r"textures ([0-9.]+) ms ([0-9.]+) MB; ([0-9.]+) on the GPU; "
    r"([0-9.]+) copying back ([0-9.]+) MB\), ([0-9.]+) draws/frame, "
    r"dropped ([0-9.]+) without a host pipeline \((\d+) being made\) and ([0-9.]+) indirect"
)
RX_CARD = re.compile(
    r"host copies in the card's memory: (\d+) copies (\d+) MB, "
    r"their bytes from before (\d+) MB; per frame: ([0-9.]+) MB sent up in ([0-9.]+) ranges, "
    r"([0-9.]+) pieces compared on the card, ([0-9.]+) of them fetched \(([0-9.]+) MB\) "
    r"with ([0-9.]+) more waits for the card \(([0-9.]+) ms\)"
)
RX_BLACK = re.compile(r"black frame (\d+):")
RX_OOM = re.compile(r"(?:error 1455|ERROR_COMMITMENT_LIMIT|out of memory|failed to commit)", re.IGNORECASE)
RX_PANIC = re.compile(
    r"(?:panicked at|fatal runtime error|STATUS_ACCESS_VIOLATION|0xc0000005|0x887a0005|"
    r"DXGI_ERROR_DEVICE_REMOVED|DXGI_ERROR_DEVICE_HUNG|DXGI_ERROR_DEVICE_RESET)",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def safe_read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def safe_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_safe_segment(seg: str) -> bool:
    if not seg or seg in (".", ".."):
        return False
    if "/" in seg or "\\" in seg or "\x00" in seg:
        return False
    return True


def number_stats(values):
    if not values:
        return {"Count": 0, "Avg": None, "Min": None, "Max": None, "P95": None}
    s = sorted(float(v) for v in values)
    avg = sum(s) / len(s)
    p95_idx = max(0, min(len(s) - 1, int(-(-0.95 * len(s) // 1)) - 1))
    return {
        "Count": len(s),
        "Avg": round(avg, 2),
        "Min": round(s[0], 2),
        "Max": round(s[-1], 2),
        "P95": round(s[p95_idx], 2),
    }


def parse_victory_log(log_text: str, world_draw_threshold: int = 500, include_series: bool = True):
    clean = ANSI_RE.sub("", log_text or "")
    lines = clean.splitlines()

    in_world = False
    world_boot_frames = None
    world_boot_sec = None
    world_boot_reason = None

    rebar_supported = None
    rebar_message = None
    card_budget_mb = None

    black_frames = 0
    oom_hits = 0
    error_lines = []
    warn_count = 0

    all_samples = []
    world_samples = []
    all_card_samples = []
    world_card_samples = []

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue

        ts_m = RX_TS.match(line)
        ts = ts_m.group(1) if ts_m else None

        if " WARN " in line:
            warn_count += 1

        m_rebar = RX_REBAR.search(line)
        if m_rebar:
            rebar_message = m_rebar.group(1).strip()
            m_heap = RX_GPU_HEAP.search(line)
            if m_heap:
                rebar_supported = m_heap.group(1) == "true"
            m_budget = RX_BUDGET.search(line)
            if m_budget:
                card_budget_mb = int(m_budget.group(1))

        if not in_world:
            m_boot = RX_FAST_BOOT.search(line)
            if m_boot:
                world_boot_frames = int(m_boot.group(1))
                world_boot_sec = float(m_boot.group(2))
                world_boot_reason = m_boot.group(3)
                if world_boot_reason == "render targets":
                    in_world = True

        if RX_BLACK.search(line):
            black_frames += 1

        if RX_OOM.search(line):
            oom_hits += 1
            if len(error_lines) < 25:
                error_lines.append({"line": idx, "kind": "oom", "text": line.strip()})
        elif RX_PANIC.search(line):
            if len(error_lines) < 25:
                error_lines.append({"line": idx, "kind": "panic", "text": line.strip()})
        elif " ERROR " in line and len(error_lines) < 25:
            error_lines.append({"line": idx, "kind": "error", "text": line.strip()})

        m_card = RX_CARD.search(line)
        if m_card:
            cs = {
                "idx": len(all_card_samples),
                "ts": ts,
                "inWorld": in_world,
                "Copies": int(m_card.group(1)),
                "CardMb": int(m_card.group(2)),
                "BeforeMb": int(m_card.group(3)),
                "SentUpMb": float(m_card.group(4)),
                "Ranges": float(m_card.group(5)),
                "Compared": float(m_card.group(6)),
                "Fetched": float(m_card.group(7)),
                "FetchedMb": float(m_card.group(8)),
                "Waits": float(m_card.group(9)),
                "WaitMs": float(m_card.group(10)),
            }
            all_card_samples.append(cs)
            if in_world:
                world_card_samples.append(cs)

        m_frame = RX_FRAME.search(line)
        if m_frame:
            draws = float(m_frame.group(11))
            if draws >= world_draw_threshold:
                in_world = True
            is_world_sample = in_world and draws >= world_draw_threshold
            cpu_prep = float(m_frame.group(3))
            copy_ms = float(m_frame.group(4))
            tex_ms = float(m_frame.group(6))
            sample = {
                "idx": len(all_samples),
                "ts": ts,
                "inWorld": is_world_sample,
                "Fps": float(m_frame.group(1)),
                "FrameMs": float(m_frame.group(2)),
                "CpuPrepMs": cpu_prep,
                "CpuOtherMs": round(max(0.0, cpu_prep - copy_ms - tex_ms), 2),
                "CopyMs": copy_ms,
                "CopyMb": float(m_frame.group(5)),
                "TextureMs": tex_ms,
                "TextureMb": float(m_frame.group(7)),
                "GpuMs": float(m_frame.group(8)),
                "CopyBackMs": float(m_frame.group(9)),
                "CopyBackMb": float(m_frame.group(10)),
                "Draws": draws,
                "Dropped": float(m_frame.group(12)),
                "Making": int(m_frame.group(13)),
                "Indirect": float(m_frame.group(14)),
            }
            all_samples.append(sample)
            if is_world_sample:
                world_samples.append(sample)

    steady_samples = world_samples[1:] if len(world_samples) >= 4 else world_samples
    steady_start_idx = steady_samples[0]["idx"] if steady_samples else None
    for s in all_samples:
        s["steady"] = steady_start_idx is not None and s["inWorld"] and s["idx"] >= steady_start_idx

    fps_stats = number_stats([s["Fps"] for s in steady_samples])
    ms_stats = number_stats([s["FrameMs"] for s in steady_samples])
    cpu_stats = number_stats([s["CpuPrepMs"] for s in steady_samples])
    copy_stats = number_stats([s["CopyMs"] for s in steady_samples])
    tex_stats = number_stats([s["TextureMs"] for s in steady_samples])
    gpu_stats = number_stats([s["GpuMs"] for s in steady_samples])
    back_stats = number_stats([s["CopyBackMs"] for s in steady_samples])
    draws_stats = number_stats([s["Draws"] for s in steady_samples])
    dropped_stats = number_stats([s["Dropped"] for s in steady_samples])
    card_mb_stats = number_stats([s["CardMb"] for s in world_card_samples])
    card_wait_stats = number_stats([s["WaitMs"] for s in world_card_samples])

    bottleneck = "unknown"
    breakdown = None
    if steady_samples:
        gpu_ms = float(gpu_stats["Avg"] or 0.0)
        copy_total = float(copy_stats["Avg"] or 0.0) + float(back_stats["Avg"] or 0.0)
        tex_ms = float(tex_stats["Avg"] or 0.0)
        cpu_other = max(0.0, float(cpu_stats["Avg"] or 0.0) - float(copy_stats["Avg"] or 0.0) - tex_ms)
        parts = [
            ("GPU-bound", gpu_ms),
            ("Copy/UMA-bound", copy_total),
            ("Texture-bound", tex_ms),
            ("CPU-prep-bound", cpu_other),
        ]
        parts.sort(key=lambda item: item[1], reverse=True)
        bottleneck = parts[0][0]
        breakdown = {
            "gpuMs": round(gpu_ms, 2),
            "copyBackMs": round(copy_total, 2),
            "textureMs": round(tex_ms, 2),
            "cpuOtherMs": round(cpu_other, 2),
        }

    result = {
        "ReachedWorld": bool(in_world and len(world_samples) > 0),
        "WorldBootFrames": world_boot_frames,
        "WorldBootSec": world_boot_sec,
        "WorldBootReason": world_boot_reason,
        "RebarSupported": rebar_supported,
        "RebarMessage": rebar_message,
        "CardBudgetMb": card_budget_mb,
        "TotalSamples": len(all_samples),
        "WorldSampleCount": len(world_samples),
        "SteadySampleCount": len(steady_samples),
        "WorldFpsAvg": fps_stats["Avg"],
        "WorldFpsMin": fps_stats["Min"],
        "WorldFpsMax": fps_stats["Max"],
        "WorldMsAvg": ms_stats["Avg"],
        "WorldMsP95": ms_stats["P95"],
        "CpuPrepMsAvg": cpu_stats["Avg"],
        "CopyMsAvg": copy_stats["Avg"],
        "TextureMsAvg": tex_stats["Avg"],
        "GpuMsAvg": gpu_stats["Avg"],
        "CopyBackMsAvg": back_stats["Avg"],
        "DrawsAvg": draws_stats["Avg"],
        "DroppedAvg": dropped_stats["Avg"],
        "CardMbMax": card_mb_stats["Max"],
        "CardWaitMsAvg": card_wait_stats["Avg"],
        "Bottleneck": bottleneck,
        "Breakdown": breakdown,
        "BlackFrames": black_frames,
        "OomHits": oom_hits,
        "WarnCount": warn_count,
        "ErrorLines": [e["text"] for e in error_lines],
        "ErrorDetails": error_lines,
        "LineCount": len(lines),
    }
    if include_series:
        result["Samples"] = all_samples
        result["CardSamples"] = all_card_samples
    return result


def get_git_info():
    def _run(args):
        try:
            out = subprocess.check_output(
                ["git"] + args,
                cwd=str(ROOT_DIR),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            return out.strip()
        except Exception:
            return ""

    sha = _run(["rev-parse", "--short", "HEAD"]) or "unknown"
    branch = _run(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    subject = _run(["log", "-1", "--pretty=%s"]) or ""
    dirty = bool(_run(["status", "--porcelain"]))
    return {"sha": sha, "branch": branch, "subject": subject, "dirty": dirty}


def load_ui_config():
    cfg = safe_read_json(UI_CONFIG_PATH, {}) or {}
    defaults = {
        "gamePath": "",
        "worldMeasureSec": 60,
        "cutsceneGraceSec": 30,
        "bootTimeoutSec": 420,
        "stallTimeoutSec": 120,
        "maxRunSec": 600,
        "cooldownSec": 10,
        "walkTest": False,
        "rebuild": False,
        "skipSetup": False,
        "skipWarm": False,
        "skipPrecompile": False,
        "noCapture": False,
    }
    defaults.update(cfg)
    return defaults


def save_ui_config(patch: dict):
    cfg = load_ui_config()
    cfg.update(patch)
    safe_write_json(UI_CONFIG_PATH, cfg)
    return cfg


def validate_game_path(path_str: str):
    if not path_str or not path_str.strip():
        return {"valid": False, "exists": False, "message": "No game path configured"}
    p = Path(path_str.strip())
    if not p.exists():
        return {"valid": False, "exists": False, "message": "Directory does not exist"}
    exe = p / "game_scarlett_master.exe"
    if not exe.exists():
        return {"valid": False, "exists": True, "message": "game_scarlett_master.exe not found in folder"}
    return {"valid": True, "exists": True, "message": "Valid Scarlett dump folder"}


def load_loop_state():
    state = safe_read_json(LOOP_STATE_PATH, None)
    if not isinstance(state, dict):
        state = {
            "mode": "manual",
            "paused": False,
            "goal": "Raise steady-state world FPS on RX 6800 XT (Series S profile) without TDRs or black frames.",
            "baselineSession": "",
            "baselineProfile": "",
            "currentStep": "idle",
            "currentHypothesis": "",
            "updatedAt": utc_now_iso(),
        }
    return state


def save_loop_state(patch: dict):
    state = load_loop_state()
    state.update(patch)
    state["updatedAt"] = utc_now_iso()
    safe_write_json(LOOP_STATE_PATH, state)
    return state


def load_experiments(limit: int = 200):
    if not EXPERIMENTS_PATH.exists():
        return []
    rows = []
    for line in safe_read_text(EXPERIMENTS_PATH).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def append_experiment(entry: dict):
    EXPERIMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = load_experiments(limit=10000)
    next_id = (existing[-1].get("id", 0) + 1) if existing else 1
    git_info = get_git_info()
    record = {
        "id": next_id,
        "timestamp": utc_now_iso(),
        "gitSha": entry.get("gitSha") or git_info["sha"],
        "gitBranch": entry.get("gitBranch") or git_info["branch"],
        "hypothesis": entry.get("hypothesis", ""),
        "sessionId": entry.get("sessionId", ""),
        "profile": entry.get("profile", ""),
        "verdict": entry.get("verdict", "PENDING"),
        "decision": entry.get("decision", "PENDING"),
        "metrics": entry.get("metrics") or {},
        "delta": entry.get("delta") or {},
        "notes": entry.get("notes", ""),
    }
    with EXPERIMENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def update_experiment_decision(exp_id: int, decision: str, notes: str = None):
    if not EXPERIMENTS_PATH.exists():
        return None
    rows = load_experiments(limit=10000)
    updated = None
    for r in rows:
        if r.get("id") == exp_id:
            r["decision"] = decision
            if notes is not None:
                r["notes"] = notes
            updated = r
            break
    if updated is not None:
        with EXPERIMENTS_PATH.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return updated


def read_profile_merged_log(profile_dir: Path) -> str:
    run_log = profile_dir / "run.log"
    if run_log.exists() and run_log.stat().st_size > 0:
        return safe_read_text(run_log)
    out = safe_read_text(profile_dir / "stdout.log")
    err = safe_read_text(profile_dir / "stderr.log")
    if out and err:
        return out + "\n" + err
    return out or err


def inspect_profile_dir(session_id: str, profile_dir: Path, include_series: bool = False):
    name = profile_dir.name
    res = safe_read_json(profile_dir / "result.json", None)
    env_cfg = safe_read_json(profile_dir / "env.json", {}) or {}
    frames_dir = profile_dir / "frames"
    frames = []
    if frames_dir.exists():
        frames = sorted(p.name for p in frames_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png")

    log_text = read_profile_merged_log(profile_dir)
    parsed = parse_victory_log(log_text, include_series=include_series) if log_text else None

    if res is None:
        # Live/in-progress profile without result.json yet
        row = {
            "Profile": name,
            "SessionId": session_id,
            "Console": "unknown",
            "Description": "",
            "Verdict": "RUNNING" if log_text else "PENDING",
            "TerminationReason": "running",
            "PeakWorkingSetGb": None,
            "PeakCommitGb": None,
            "TdrCount": 0,
            "CrashDeltaLines": 0,
            "CapturedPngCount": len(frames),
            "ExitCode": None,
            "ElapsedSec": None,
            "OverridesText": "",
            "Live": True,
        }
        if parsed:
            row.update(parsed)
    else:
        row = dict(res)
        row["Profile"] = res.get("Profile") or name
        row["SessionId"] = session_id
        row["CapturedPngCount"] = len(frames) if frames else int(res.get("CapturedPngCount") or 0)
        row["Live"] = False
        if parsed:
            row["Breakdown"] = parsed.get("Breakdown")
            row["WarnCount"] = parsed.get("WarnCount", 0)
            row["ErrorDetails"] = parsed.get("ErrorDetails", [])
            row["LineCount"] = parsed.get("LineCount", 0)
            if include_series:
                row["Samples"] = parsed.get("Samples", [])
                row["CardSamples"] = parsed.get("CardSamples", [])

    row["Frames"] = frames
    row["Env"] = env_cfg
    row["HasPressFile"] = (profile_dir / "press.txt").exists()
    return row


def list_sessions():
    if not HARNESS_LOGS_DIR.exists():
        return []
    sessions = []
    for d in sorted(HARNESS_LOGS_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        host = safe_read_json(d / "host.json", {}) or {}
        matrix = safe_read_json(d / "matrix.json", {}) or {}
        profile_dirs = [p for p in sorted(d.iterdir(), key=lambda x: x.name) if p.is_dir()]
        profiles = [inspect_profile_dir(d.name, p, include_series=False) for p in profile_dirs]
        verdicts = collections.Counter(p.get("Verdict", "UNKNOWN") for p in profiles)
        passing = [p for p in profiles if p.get("Verdict") == "PASS" and p.get("WorldFpsAvg") is not None]
        best = max(passing, key=lambda p: float(p["WorldFpsAvg"])) if passing else None
        mtime = datetime.datetime.fromtimestamp(d.stat().st_mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sessions.append(
            {
                "id": d.name,
                "updatedAt": mtime,
                "host": host,
                "profileCount": len(profiles),
                "expectedCount": len(matrix.get("profiles", [])) if isinstance(matrix, dict) else len(profiles),
                "verdicts": dict(verdicts),
                "bestProfile": best["Profile"] if best else None,
                "bestFps": best["WorldFpsAvg"] if best else None,
                "bestBottleneck": best.get("Bottleneck") if best else None,
                "hasSummaryMd": (d / "SUMMARY.md").exists(),
                "hasSummaryCsv": (d / "summary.csv").exists(),
            }
        )
    return sessions


def get_session_detail(session_id: str):
    if not is_safe_segment(session_id):
        return None
    sdir = HARNESS_LOGS_DIR / session_id
    if not sdir.exists() or not sdir.is_dir():
        return None
    host = safe_read_json(sdir / "host.json", {}) or {}
    matrix = safe_read_json(sdir / "matrix.json", {}) or {}
    profile_dirs = [p for p in sorted(sdir.iterdir(), key=lambda x: x.name) if p.is_dir()]
    profiles = [inspect_profile_dir(session_id, p, include_series=False) for p in profile_dirs]
    return {
        "id": session_id,
        "path": str(sdir),
        "host": host,
        "matrix": matrix,
        "profiles": profiles,
        "summaryMd": safe_read_text(sdir / "SUMMARY.md"),
    }


def generate_demo_session(session_id: str = "demo-selftest"):
    """Create a realistic synthetic multi-profile session for immediate UI exploration & self-testing."""
    sdir = HARNESS_LOGS_DIR / session_id
    if sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)
    sdir.mkdir(parents=True, exist_ok=True)

    host_info = {
        "GpuName": "AMD Radeon RX 6800 XT (16 GB)",
        "GpuDriver": "32.0.12033.1030",
        "CpuName": "AMD Ryzen 5 7600X 6-Core Processor",
        "RamGb": 32.0,
        "PagefileGb": 32.0,
        "OsCaption": "Microsoft Windows 10 Pro",
        "OsBuild": "19045",
    }
    safe_write_json(sdir / "host.json", host_info)
    matrix = safe_read_json(MATRIX_PATH, {"profiles": []})
    safe_write_json(sdir / "matrix.json", matrix)

    demo_profiles = [
        {
            "name": "01-baseline-series-s",
            "console": "series-s",
            "desc": "Synthetic baseline (GPU-bound ~5.2 fps, ReBAR unavailable)",
            "overrides": "",
            "env": {"VICTORY_OWN_UPSCALER": "1", "VICTORY_RELAY": "2"},
            "verdict": "PASS",
            "term": "completed",
            "ram": 12.1,
            "commit": 20.4,
            "tdr": 0,
            "boot_s": 74.2,
            "rebar": "false",
            "samples": [
                (3.1, 322.5, 60.0, 22.0, 45.0, 18.0, 12.0, 240.0, 14.0, 2.1, 4820, 12, 3, 110),
                (5.0, 200.0, 34.0, 14.0, 28.0, 8.0, 4.0, 150.0, 8.0, 1.2, 5100, 0, 0, 120),
                (5.4, 185.2, 30.0, 12.0, 24.0, 6.0, 3.0, 140.0, 6.0, 0.9, 5120, 0, 0, 120),
                (5.2, 192.3, 32.0, 13.0, 26.0, 7.0, 3.5, 146.0, 7.0, 1.0, 5090, 0, 0, 120),
                (5.3, 188.7, 31.0, 12.5, 25.0, 6.5, 3.2, 143.0, 6.5, 1.0, 5110, 0, 0, 120),
            ],
            "card_mb": 1620,
            "wait_ms": 3.4,
        },
        {
            "name": "02-vram-4gb-series-s",
            "console": "series-s",
            "desc": "Synthetic 4 GiB VRAM card buffers (lower copy overhead, ~6.4 fps)",
            "overrides": "VICTORY_CARD_BUFFERS_MB=4096 VICTORY_CARD_BUFFERS_MIN_MB=2",
            "env": {"VICTORY_CARD_BUFFERS_MB": "4096", "VICTORY_CARD_BUFFERS_MIN_MB": "2"},
            "verdict": "PASS",
            "term": "completed",
            "ram": 12.4,
            "commit": 21.1,
            "tdr": 0,
            "boot_s": 69.8,
            "rebar": "false",
            "samples": [
                (4.0, 250.0, 42.0, 14.0, 26.0, 11.0, 6.0, 192.0, 8.0, 1.1, 4950, 4, 1, 115),
                (6.2, 161.3, 22.0, 7.2, 14.0, 5.5, 2.5, 132.0, 4.1, 0.6, 5120, 0, 0, 122),
                (6.5, 153.8, 20.5, 6.8, 12.5, 5.2, 2.2, 126.0, 3.8, 0.5, 5140, 0, 0, 122),
                (6.4, 156.2, 21.0, 7.0, 13.0, 5.4, 2.4, 128.0, 4.0, 0.5, 5130, 0, 0, 122),
                (6.6, 151.5, 19.8, 6.4, 12.0, 5.0, 2.1, 124.0, 3.6, 0.4, 5150, 0, 0, 122),
            ],
            "card_mb": 3480,
            "wait_ms": 1.6,
        },
        {
            "name": "04-indirect-barrier-series-s",
            "console": "series-s",
            "desc": "Synthetic indirect + barrier merge + rehash runs (~7.9 fps winner)",
            "overrides": "VICTORY_CARD_BUFFERS_MB=4096 VICTORY_CARD_REHASH_RUNS=1 VICTORY_INDIRECT_WINDOW=1 VICTORY_INDIRECT_MERGE=1 VICTORY_BARRIER_MERGE=1",
            "env": {
                "VICTORY_CARD_BUFFERS_MB": "4096",
                "VICTORY_CARD_REHASH_RUNS": "1",
                "VICTORY_INDIRECT_WINDOW": "1",
                "VICTORY_INDIRECT_MERGE": "1",
                "VICTORY_BARRIER_MERGE": "1",
            },
            "verdict": "PASS",
            "term": "completed",
            "ram": 12.6,
            "commit": 21.5,
            "tdr": 0,
            "boot_s": 66.1,
            "rebar": "true",
            "samples": [
                (5.2, 192.3, 30.0, 9.0, 16.0, 8.0, 4.0, 151.0, 5.0, 0.7, 5020, 2, 0, 92),
                (7.8, 128.2, 15.5, 4.5, 9.0, 4.2, 1.8, 108.0, 2.8, 0.3, 5140, 0, 0, 88),
                (8.1, 123.5, 14.8, 4.1, 8.4, 4.0, 1.6, 104.0, 2.5, 0.3, 5160, 0, 0, 86),
                (7.9, 126.6, 15.1, 4.3, 8.8, 4.1, 1.7, 106.5, 2.6, 0.3, 5150, 0, 0, 87),
                (8.0, 125.0, 14.9, 4.2, 8.6, 4.0, 1.7, 105.2, 2.6, 0.3, 5155, 0, 0, 86),
            ],
            "card_mb": 3590,
            "wait_ms": 0.9,
        },
        {
            "name": "06-series-x-baseline",
            "console": "series-x",
            "desc": "Synthetic Series X stress test (simulated TDR / device removed)",
            "overrides": "VICTORY_OWN_UPSCALER=0 VICTORY_CARD_BUFFERS_MB=3072",
            "env": {"VICTORY_OWN_UPSCALER": "0", "VICTORY_CARD_BUFFERS_MB": "3072"},
            "verdict": "TDR",
            "term": "exited-early",
            "ram": 16.8,
            "commit": 29.9,
            "tdr": 1,
            "boot_s": 91.4,
            "rebar": "false",
            "samples": [
                (2.1, 476.2, 72.0, 28.0, 62.0, 22.0, 15.0, 382.0, 19.0, 3.2, 6420, 18, 4, 150),
                (2.4, 416.7, 64.0, 25.0, 55.0, 18.0, 12.0, 334.0, 16.0, 2.8, 6510, 6, 1, 152),
            ],
            "card_mb": 3070,
            "wait_ms": 8.2,
            "extra_err": "2026-11-25T14:22:18Z ERROR victory_gpu::submit: DXGI_ERROR_DEVICE_HUNG (0x887a0006) during pass 412",
        },
    ]

    for dp in demo_profiles:
        pdir = sdir / dp["name"]
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "frames").mkdir(exist_ok=True)
        safe_write_json(pdir / "env.json", dp["env"])

        rebar_msg = (
            "AVAILABLE (3072 MB GPU upload heaps active)"
            if dp["rebar"] == "true"
            else "NOT available: VICTORY_REBAR is ignored, copies are made as before"
        )
        log_lines = [
            f"2026-11-25T14:10:01Z INFO victory_gpu::rebar: gpu_upload_heaps={dp['rebar']} options16_known=true card_budget_mb=Some(15234) level=1 most_mb=3072 Resizable BAR (D3D12 GPU upload heaps): {rebar_msg}",
            "2026-11-25T14:10:25Z INFO victory_gpu::host: 30.0 fps, 33.3 ms/frame (2.1 preparing: copies 0.4 ms 1.2 MB, textures 0.2 ms 0.5 MB; 1.8 on the GPU; 0.1 copying back 0.0 MB), 42 draws/frame, dropped 0 without a host pipeline (0 being made) and 0 indirect",
            f"2026-11-25T14:11:15Z WARN victory_gpu::queue: fast boot: in world after 1240 frames, {dp['boot_s']} s (render targets)",
        ]
        sec = 25
        for s in dp["samples"]:
            ts = f"2026-11-25T14:11:{sec:02d}Z"
            sec = min(59, sec + 8)
            log_lines.append(
                f"{ts} INFO victory_gpu::host: host copies in the card's memory: 18 copies {dp['card_mb']} MB, "
                f"their bytes from before {dp['card_mb'] // 2} MB; per frame: {s[4]} MB sent up in 62 ranges, "
                f"320 pieces compared on the card, 12 of them fetched (0.8 MB) with 1.0 more waits for the card ({dp['wait_ms']} ms)"
            )
            log_lines.append(
                f"{ts} INFO victory_gpu::host: {s[0]} fps, {s[1]} ms/frame "
                f"({s[2]} preparing: copies {s[3]} ms {s[4]} MB, textures {s[5]} ms {s[6]} MB; "
                f"{s[7]} on the GPU; {s[8]} copying back {s[9]} MB), {s[10]} draws/frame, "
                f"dropped {s[11]} without a host pipeline ({s[12]} being made) and {s[13]} indirect"
            )
        if dp.get("extra_err"):
            log_lines.append(dp["extra_err"])

        log_text = "\n".join(log_lines) + "\n"
        (pdir / "run.log").write_text(log_text, encoding="utf-8")
        parsed = parse_victory_log(log_text, include_series=False)
        res = dict(parsed)
        res.update(
            {
                "Profile": dp["name"],
                "Description": dp["desc"],
                "Console": dp["console"],
                "Verdict": dp["verdict"],
                "TerminationReason": dp["term"],
                "PeakWorkingSetGb": dp["ram"],
                "PeakCommitGb": dp["commit"],
                "TdrCount": dp["tdr"],
                "CrashDeltaLines": 1 if dp["tdr"] else 0,
                "CapturedPngCount": 0,
                "ExitCode": -2005270522 if dp["tdr"] else 0,
                "ElapsedSec": round(dp["boot_s"] + 90.0, 1),
                "OverridesText": dp["overrides"],
            }
        )
        safe_write_json(pdir / "result.json", res)

    return get_session_detail(session_id)


class HarnessRunner:
    """Manages a single active scripts/harness.ps1 child process + live output ring buffer + SSE broadcast."""

    def __init__(self):
        self._lock = threading.Lock()
        self.proc = None
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.command = []
        self.active_session_id = None
        self.active_profile = None
        self.phase = "idle"
        self.output_lines = collections.deque(maxlen=600)
        self.subscribers = []

    def subscribe(self):
        q = collections.deque(maxlen=100)
        ev = threading.Event()
        with self._lock:
            self.subscribers.append((q, ev))
        return q, ev

    def unsubscribe(self, pair):
        with self._lock:
            if pair in self.subscribers:
                self.subscribers.remove(pair)

    def broadcast(self, event_name: str, payload: dict):
        msg = (event_name, payload)
        with self._lock:
            for q, ev in list(self.subscribers):
                q.append(msg)
                ev.set()

    def status(self):
        with self._lock:
            elapsed = None
            if self.running and self.started_at is not None:
                elapsed = round(time.time() - self.started_at, 1)
            return {
                "running": self.running,
                "pid": self.proc.pid if (self.proc and self.running) else None,
                "phase": self.phase,
                "activeSessionId": self.active_session_id,
                "activeProfile": self.active_profile,
                "elapsedSec": elapsed,
                "exitCode": self.exit_code,
                "command": self.command,
                "recentOutput": list(self.output_lines)[-120:],
            }

    def start(self, params: dict):
        with self._lock:
            if self.running:
                raise RuntimeError("A harness run is already in progress.")

            game_path = (params.get("gamePath") or "").strip()
            analyze_only = (params.get("analyzeOnly") or "").strip()
            self_test = bool(params.get("selfTest"))

            ps_cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(HARNESS_PS1),
            ]

            custom_matrix_path = None
            if self_test:
                ps_cmd.append("-SelfTest")
            elif analyze_only:
                if not is_safe_segment(analyze_only):
                    raise ValueError("Invalid session name for analyzeOnly")
                ps_cmd.extend(["-AnalyzeOnly", str(HARNESS_LOGS_DIR / analyze_only)])
            else:
                if not game_path:
                    raise ValueError("gamePath is required.")
                val = validate_game_path(game_path)
                if not val["valid"]:
                    raise ValueError(val["message"])
                ps_cmd.extend(["-Game", game_path])

                # Optional ad-hoc custom profile run
                custom_profile = params.get("customProfile")
                if isinstance(custom_profile, dict) and custom_profile.get("name"):
                    base_matrix = safe_read_json(MATRIX_PATH, {"defaults": {"console": "series-s", "env": {}}, "profiles": []})
                    TMP_CUSTOM_MATRIX_DIR.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(custom_profile["name"])).strip("-") or "custom-run"
                    custom_matrix_path = TMP_CUSTOM_MATRIX_DIR / f"{stamp}-{safe_name}.json"
                    custom_obj = {
                        "description": f"Ad-hoc custom profile {safe_name}",
                        "defaults": base_matrix.get("defaults", {"console": "series-s", "env": {}}),
                        "profiles": [
                            {
                                "name": safe_name,
                                "description": str(custom_profile.get("description") or "Ad-hoc UI/Agent experiment"),
                                "console": str(custom_profile.get("console") or "series-s"),
                                "env": custom_profile.get("env") or {},
                            }
                        ],
                    }
                    safe_write_json(custom_matrix_path, custom_obj)
                    ps_cmd.extend(["-Matrix", str(custom_matrix_path)])
                else:
                    profiles = params.get("profiles") or []
                    if profiles:
                        ps_cmd.extend(["-Only", ",".join(str(p) for p in profiles)])

                resume_id = (params.get("resume") or "").strip()
                if resume_id:
                    if not is_safe_segment(resume_id):
                        raise ValueError("Invalid resume session ID")
                    ps_cmd.extend(["-Resume", str(HARNESS_LOGS_DIR / resume_id)])

                int_flags = [
                    ("worldMeasureSec", "-WorldMeasureSec"),
                    ("cutsceneGraceSec", "-CutsceneGraceSec"),
                    ("bootTimeoutSec", "-BootTimeoutSec"),
                    ("stallTimeoutSec", "-StallTimeoutSec"),
                    ("maxRunSec", "-MaxRunSec"),
                    ("cooldownSec", "-CooldownSec"),
                ]
                for key, flag in int_flags:
                    if params.get(key) is not None and str(params.get(key)) != "":
                        ps_cmd.extend([flag, str(int(params[key]))])

                bool_flags = [
                    ("walkTest", "-WalkTest"),
                    ("rebuild", "-Rebuild"),
                    ("skipSetup", "-SkipSetup"),
                    ("skipWarm", "-SkipWarm"),
                    ("skipPrecompile", "-SkipPrecompile"),
                    ("noCapture", "-NoCapture"),
                ]
                for key, flag in bool_flags:
                    if bool(params.get(key)):
                        ps_cmd.append(flag)

            self.output_lines.clear()
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.exit_code = None
            self.command = ps_cmd
            self.active_session_id = (params.get("resume") or "").strip() or None
            self.active_profile = None
            self.phase = "starting"

            self.proc = subprocess.Popen(
                ps_cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

        self.broadcast("status", self.status())
        t = threading.Thread(target=self._reader_thread, daemon=True)
        t.start()
        return self.status()

    def _reader_thread(self):
        rx_session = re.compile(r"Session:\s+(.+)$")
        rx_running = re.compile(r"Running \[([^\]]+)\]")
        try:
            assert self.proc is not None and self.proc.stdout is not None
            for raw_line in self.proc.stdout:
                line = ANSI_RE.sub("", raw_line.rstrip("\r\n"))
                with self._lock:
                    self.output_lines.append(line)
                    m_s = rx_session.search(line)
                    if m_s:
                        self.active_session_id = Path(m_s.group(1).strip()).name
                    m_r = rx_running.search(line)
                    if m_r:
                        self.active_profile = m_r.group(1).strip()
                        self.phase = "booting"
                    elif "Building victory.exe" in line:
                        self.phase = "building"
                    elif "Warming shader cache" in line:
                        self.phase = "warming-shaders"
                    elif "Precompiling graphics pipelines" in line:
                        self.phase = "precompiling"
                    elif "World detected!" in line:
                        self.phase = "in-world"
                    elif "Cooling down" in line:
                        self.phase = "cooldown"
                    elif "Harness finished!" in line:
                        self.phase = "finished"
                self.broadcast("line", {"line": line, "phase": self.phase, "activeSessionId": self.active_session_id, "activeProfile": self.active_profile})
        finally:
            code = self.proc.wait() if self.proc else -1
            with self._lock:
                self.running = False
                self.finished_at = time.time()
                self.exit_code = code
                self.phase = "idle"
            self.broadcast("status", self.status())

    def stop(self):
        with self._lock:
            if not self.running or not self.proc:
                return {"stopped": False, "message": "No active run"}
            pid = self.proc.pid
            self.phase = "stopping"
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.broadcast("status", self.status())
        return {"stopped": True, "pid": pid}

    def send_press(self, tokens: str, session_id: str = None, profile: str = None):
        sid = session_id or self.active_session_id
        prof = profile or self.active_profile
        if not sid or not prof or not is_safe_segment(sid) or not is_safe_segment(prof):
            raise ValueError("No active session/profile available for press.txt injection")
        press_path = HARNESS_LOGS_DIR / sid / prof / "press.txt"
        if not press_path.parent.exists():
            raise ValueError(f"Profile folder not found: {sid}/{prof}")
        press_path.write_text(tokens.strip(), encoding="utf-8")
        return {"ok": True, "path": str(press_path), "tokens": tokens.strip()}


RUNNER = HarnessRunner()


def build_agent_snapshot():
    """Compact high-signal diagnostic JSON designed for an autonomous optimization loop."""
    sessions = list_sessions()
    latest_summary = sessions[0] if sessions else None
    latest_detail = get_session_detail(latest_summary["id"]) if latest_summary else None
    loop_state = load_loop_state()
    experiments = load_experiments(limit=25)
    cfg = load_ui_config()

    compact_profiles = []
    if latest_detail:
        for p in latest_detail["profiles"]:
            compact_profiles.append(
                {
                    "profile": p.get("Profile"),
                    "console": p.get("Console"),
                    "verdict": p.get("Verdict"),
                    "bootSec": p.get("WorldBootSec"),
                    "fpsAvg": p.get("WorldFpsAvg"),
                    "fpsMin": p.get("WorldFpsMin"),
                    "fpsMax": p.get("WorldFpsMax"),
                    "frameMsAvg": p.get("WorldMsAvg"),
                    "frameMsP95": p.get("WorldMsP95"),
                    "gpuMsAvg": p.get("GpuMsAvg"),
                    "copyMsAvg": p.get("CopyMsAvg"),
                    "copyBackMsAvg": p.get("CopyBackMsAvg"),
                    "textureMsAvg": p.get("TextureMsAvg"),
                    "cpuPrepMsAvg": p.get("CpuPrepMsAvg"),
                    "drawsAvg": p.get("DrawsAvg"),
                    "droppedAvg": p.get("DroppedAvg"),
                    "cardMbMax": p.get("CardMbMax"),
                    "cardWaitMsAvg": p.get("CardWaitMsAvg"),
                    "peakRamGb": p.get("PeakWorkingSetGb"),
                    "peakCommitGb": p.get("PeakCommitGb"),
                    "rebarSupported": p.get("RebarSupported"),
                    "tdrCount": p.get("TdrCount"),
                    "blackFrames": p.get("BlackFrames"),
                    "bottleneck": p.get("Bottleneck"),
                    "overrides": p.get("OverridesText"),
                    "errorLines": (p.get("ErrorLines") or [])[:5],
                    "capturedPngs": p.get("CapturedPngCount", 0),
                }
            )

    return {
        "generatedAt": utc_now_iso(),
        "git": get_git_info(),
        "gamePathValid": validate_game_path(cfg.get("gamePath", ""))["valid"],
        "runner": {
            "running": RUNNER.status()["running"],
            "phase": RUNNER.status()["phase"],
            "activeSessionId": RUNNER.status()["activeSessionId"],
            "activeProfile": RUNNER.status()["activeProfile"],
        },
        "loop": loop_state,
        "latestSession": {
            "id": latest_detail["id"],
            "host": latest_detail["host"],
            "profiles": compact_profiles,
        }
        if latest_detail
        else None,
        "recentExperiments": experiments[-10:],
    }


class HarnessHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep console clean unless verbose
        pass

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 2 * 1024 * 1024))
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON body")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                if not UI_HTML_PATH.exists():
                    self._send_json({"error": "harness-ui.html missing"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(UI_HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                return

            if path == "/api/state":
                cfg = load_ui_config()
                self._send_json(
                    {
                        "git": get_git_info(),
                        "config": cfg,
                        "gameValidation": validate_game_path(cfg.get("gamePath", "")),
                        "matrix": safe_read_json(MATRIX_PATH, {"profiles": []}),
                        "runner": RUNNER.status(),
                        "loop": load_loop_state(),
                        "sessions": list_sessions(),
                    }
                )
                return

            if path == "/api/agent/snapshot":
                self._send_json(build_agent_snapshot())
                return

            if path == "/api/sessions":
                self._send_json({"sessions": list_sessions()})
                return

            if path == "/api/loop/state":
                self._send_json({"loop": load_loop_state(), "experiments": load_experiments()})
                return

            if path == "/api/events":
                self._handle_sse()
                return

            parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]

            # /api/sessions/<sessionId>
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "sessions":
                detail = get_session_detail(parts[2])
                if not detail:
                    self._send_json({"error": "Session not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(detail)
                return

            # /api/sessions/<sessionId>/profiles/<profile>
            if len(parts) == 5 and parts[0] == "api" and parts[1] == "sessions" and parts[3] == "profiles":
                sid, prof = parts[2], parts[4]
                if not (is_safe_segment(sid) and is_safe_segment(prof)):
                    self._send_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
                    return
                pdir = HARNESS_LOGS_DIR / sid / prof
                if not pdir.exists() or not pdir.is_dir():
                    self._send_json({"error": "Profile not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(inspect_profile_dir(sid, pdir, include_series=True))
                return

            # /api/sessions/<sessionId>/profiles/<profile>/log
            if len(parts) == 6 and parts[0] == "api" and parts[1] == "sessions" and parts[3] == "profiles" and parts[5] == "log":
                sid, prof = parts[2], parts[4]
                if not (is_safe_segment(sid) and is_safe_segment(prof)):
                    self._send_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
                    return
                pdir = HARNESS_LOGS_DIR / sid / prof
                if not pdir.exists():
                    self._send_json({"error": "Profile not found"}, HTTPStatus.NOT_FOUND)
                    return
                text = ANSI_RE.sub("", read_profile_merged_log(pdir))
                lines = text.splitlines()
                filt = (qs.get("filter", ["all"])[0] or "all").lower()
                q = (qs.get("q", [""])[0] or "").lower()
                tail = int(qs.get("tail", ["400"])[0] or "400")
                matched = []
                for idx, line in enumerate(lines, start=1):
                    if filt == "errors" and not (" ERROR " in line or " WARN " in line or RX_OOM.search(line) or RX_PANIC.search(line)):
                        continue
                    elif filt == "host" and "ms/frame" not in line:
                        continue
                    elif filt == "card" and "host copies in the card's memory" not in line:
                        continue
                    elif filt == "rebar" and "Resizable BAR" not in line and "fast boot" not in line:
                        continue
                    elif filt == "black" and "black frame" not in line:
                        continue
                    if q and q not in line.lower():
                        continue
                    matched.append({"line": idx, "text": line})
                total = len(matched)
                if tail > 0 and len(matched) > tail:
                    matched = matched[-tail:]
                self._send_json({"totalLines": len(lines), "matchedLines": total, "lines": matched})
                return

            # /api/sessions/<sessionId>/profiles/<profile>/frames/<filename>
            if len(parts) == 7 and parts[0] == "api" and parts[1] == "sessions" and parts[3] == "profiles" and parts[5] == "frames":
                sid, prof, fname = parts[2], parts[4], parts[6]
                if not (is_safe_segment(sid) and is_safe_segment(prof) and is_safe_segment(fname)):
                    self._send_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
                    return
                fpath = HARNESS_LOGS_DIR / sid / prof / "frames" / fname
                if not fpath.exists() or not fpath.is_file():
                    self._send_json({"error": "Frame not found"}, HTTPStatus.NOT_FOUND)
                    return
                ctype = mimetypes.guess_type(str(fpath))[0] or "image/png"
                self._send_bytes(fpath.read_bytes(), ctype)
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, HTTPStatus.NOT_FOUND)
        except ValueError as e:
            self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json_body()

            if path == "/api/config":
                cfg = save_ui_config(body)
                self._send_json({"config": cfg, "gameValidation": validate_game_path(cfg.get("gamePath", ""))})
                return

            if path == "/api/runs/start":
                if body.get("gamePath"):
                    save_ui_config({"gamePath": body["gamePath"]})
                st = RUNNER.start(body)
                self._send_json({"ok": True, "runner": st})
                return

            if path == "/api/runs/stop":
                res = RUNNER.stop()
                self._send_json(res)
                return

            if path == "/api/press":
                tokens = str(body.get("tokens") or "").strip()
                if not tokens:
                    raise ValueError("tokens cannot be empty")
                res = RUNNER.send_press(tokens, body.get("sessionId"), body.get("profile"))
                self._send_json(res)
                return

            if path == "/api/demo":
                sid = str(body.get("sessionId") or "demo-selftest")
                if not is_safe_segment(sid):
                    raise ValueError("Invalid sessionId")
                detail = generate_demo_session(sid)
                RUNNER.broadcast("sessions", {"action": "demo", "sessionId": sid})
                self._send_json({"ok": True, "session": detail})
                return

            if path == "/api/loop/control":
                allowed = {"mode", "paused", "goal", "baselineSession", "baselineProfile", "currentStep", "currentHypothesis"}
                patch = {k: v for k, v in body.items() if k in allowed}
                state = save_loop_state(patch)
                RUNNER.broadcast("loop", state)
                self._send_json({"ok": True, "loop": state})
                return

            if path == "/api/loop/iterations":
                rec = append_experiment(body)
                RUNNER.broadcast("experiment", rec)
                self._send_json({"ok": True, "experiment": rec})
                return

            if path == "/api/loop/iterations/decision":
                exp_id = int(body.get("id"))
                decision = str(body.get("decision") or "PENDING")
                notes = body.get("notes")
                updated = update_experiment_decision(exp_id, decision, notes)
                if not updated:
                    self._send_json({"error": "Experiment not found"}, HTTPStatus.NOT_FOUND)
                    return
                RUNNER.broadcast("experiment", updated)
                self._send_json({"ok": True, "experiment": updated})
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, HTTPStatus.NOT_FOUND)
        except ValueError as e:
            self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, HTTPStatus.CONFLICT)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        pair = RUNNER.subscribe()
        q, ev = pair
        try:
            init = f"event: status\ndata: {json.dumps(RUNNER.status())}\n\n".encode("utf-8")
            self.wfile.write(init)
            self.wfile.flush()
            while True:
                ev.wait(timeout=15.0)
                ev.clear()
                if not q:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                while q:
                    ename, payload = q.popleft()
                    chunk = f"event: {ename}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            RUNNER.unsubscribe(pair)


def run_self_test():
    """Automated backend smoke test: parser, demo generator, agent snapshot, and HTTP API."""
    import urllib.request

    # 1. Parser test
    sample_log = (
        "2026-11-25T13:10:01Z INFO victory_gpu::rebar: gpu_upload_heaps=false options16_known=true "
        "card_budget_mb=Some(15234) level=1 most_mb=3072 Resizable BAR (D3D12 GPU upload heaps): NOT available\n"
        "2026-11-25T13:11:15Z WARN victory_gpu::queue: fast boot: in world after 1240 frames, 74.2 s (render targets)\n"
        "2026-11-25T13:11:25Z INFO victory_gpu::host: 3.1 fps, 322.5 ms/frame (60.0 preparing: copies 22.0 ms 45.0 MB, "
        "textures 18.0 ms 12.0 MB; 240.0 on the GPU; 14.0 copying back 2.1 MB), 4820 draws/frame, dropped 12 without a host pipeline (3 being made) and 110 indirect\n"
        "2026-11-25T13:11:35Z INFO victory_gpu::host: 5.0 fps, 200.0 ms/frame (34.0 preparing: copies 14.0 ms 28.0 MB, "
        "textures 8.0 ms 4.0 MB; 150.0 on the GPU; 8.0 copying back 1.2 MB), 5100 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect\n"
        "2026-11-25T13:11:45Z INFO victory_gpu::host: 5.4 fps, 185.2 ms/frame (30.0 preparing: copies 12.0 ms 24.0 MB, "
        "textures 6.0 ms 3.0 MB; 140.0 on the GPU; 6.0 copying back 0.9 MB), 5120 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect\n"
        "2026-11-25T13:11:55Z INFO victory_gpu::host: 5.2 fps, 192.3 ms/frame (32.0 preparing: copies 13.0 ms 26.0 MB, "
        "textures 7.0 ms 3.5 MB; 146.0 on the GPU; 7.0 copying back 1.0 MB), 5090 draws/frame, dropped 0 without a host pipeline (0 being made) and 120 indirect\n"
    )
    parsed = parse_victory_log(sample_log, include_series=True)
    assert parsed["ReachedWorld"] is True
    assert parsed["SteadySampleCount"] == 3
    assert parsed["Bottleneck"] == "GPU-bound"
    assert abs(parsed["WorldFpsAvg"] - 5.2) < 0.05
    assert len(parsed["Samples"]) == 4

    # 2. Spin up ephemeral HTTP server and test endpoints
    srv = ThreadingHTTPServer(("127.0.0.1", 0), HarnessHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    demo_id = "selftest-ephemeral"
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/api/state", timeout=5) as r:
            st = json.loads(r.read().decode("utf-8"))
            assert "matrix" in st and "runner" in st and "loop" in st

        req = urllib.request.Request(
            f"{base}/api/demo",
            data=json.dumps({"sessionId": demo_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            demo_resp = json.loads(r.read().decode("utf-8"))
            assert demo_resp["ok"] is True
            assert len(demo_resp["session"]["profiles"]) == 4

        with urllib.request.urlopen(f"{base}/api/sessions/{demo_id}/profiles/04-indirect-barrier-series-s", timeout=5) as r:
            prof = json.loads(r.read().decode("utf-8"))
            assert prof["Verdict"] == "PASS"
            assert len(prof["Samples"]) >= 5

        with urllib.request.urlopen(f"{base}/api/agent/snapshot", timeout=5) as r:
            snap = json.loads(r.read().decode("utf-8"))
            assert "git" in snap and "loop" in snap and snap["latestSession"] is not None

        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert "ScarlettPort Harness" in html
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(HARNESS_LOGS_DIR / demo_id, ignore_errors=True)

    print("PASS: harness-ui-server parser, demo generator, REST API, and agent snapshot verified.")


def main():
    parser = argparse.ArgumentParser(description="ScarlettPort Harness UI Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default 8765)")
    parser.add_argument("--game", default="", help="Optional initial game dump folder to save in UI config")
    parser.add_argument("--self-test", action="store_true", help="Run automated backend & API self-test and exit")
    args = parser.parse_args()

    HARNESS_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if args.game:
        save_ui_config({"gamePath": args.game})

    if args.self_test:
        run_self_test()
        return

    server = ThreadingHTTPServer((args.host, args.port), HarnessHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"ScarlettPort Harness UI running at {url}", flush=True)
    print(f"Agent snapshot API: {url}/api/agent/snapshot", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Harness UI...", flush=True)
    finally:
        if RUNNER.running:
            RUNNER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
