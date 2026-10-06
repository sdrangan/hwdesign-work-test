"""fir_build.py -- the build pipeline for the command-driven FIR accelerator.

    python fir_build.py --list-steps
    python fir_build.py --through check_pysim       # Python only, no Vitis
    python fir_build.py --through summary           # everything, Vitis included

The pipeline, in reading order (every Vitis step runs at both word widths, 32 and 64):

    worked_examples  the hand-computed values that pin fir_eval down
    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    py_model         run the pure bit-exact model on every scenario, in session order
    check_model      ...and compare it with the expected response
    py_sim           run pysim (the timing model) on the well-formed scenarios
    check_pysim      ...compare
    extract_py_timing  pysim's cycle counts for the timing scenario's PROCESS commands
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place (a build outside this directory)
    gen_kernel       the kernel boundary (gen/fir.hpp, gen/fir.cpp): tops fir and fir_bw64;
                     the body is the hand-written fir_body_impl.tpp
    csim             Vitis C simulation, every scenario, hand-written fir_tb.cpp
    check_csim       ...compare
    csynth           C synthesis, then RTL co-simulation of every scenario (port trace)
    inspect_synth    loops, II, timing and resources from the synthesis report
    check_cosim      the co-simulated responses, compared
    vcd              export the cosim waveform as a VCD
    cosim_timing     latency, input rate and cycles per command, from the VCD; timing figure
    validate_timing  cosim cycles vs the pysim estimate, tolerance 20 cycles
    summary          every check, the synthesis report and the timing verdict

Every check compares against the same expected response, computed from each scenario's
intent (scenarios.py) rather than from any implementation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_bursts, write_bursts

import scenarios as S
from fir import (
    SCHEMA_CLASSES, WORD_BW_SUPPORTED, FirAccel, FirState, FirTB, Int16, connect,
    fir_stream_model,
)

_SOURCE_DIR = Path(__file__).resolve().parent

#: The hand-written sources Vitis needs beside the generated ones.
HAND_WRITTEN = ("run.tcl", "fir_tb.cpp", "fir_body_impl.tpp")
WIDTHS = tuple(WORD_BW_SUPPORTED)
TOPS = {32: "fir", 64: "fir_bw64"}
#: The timing scenario's PROCESS commands, by job index within the scenario: nsamp.
TIMING_JOBS = {1: 64, 2: 1024}


def _ensure_sources(root: Path) -> None:
    for name in HAND_WRITTEN:
        if not (root / name).exists():
            shutil.copy(_SOURCE_DIR / name, root / name)


_STUB_MARK = "TODO: implement body"


def _require_real_body(root: Path) -> None:
    body = root / "fir_body_impl.tpp"
    if _STUB_MARK in body.read_text(encoding="utf-8"):
        raise RuntimeError(f"{body} is the generated stub, not the kernel body")


@dataclass(kw_only=True)
class SourcesStep(BuildStep):
    description = "Copy the hand-written body, testbench and Tcl into the build directory."
    consumes = ["fir_source"]
    produces = {"kernel_sources": Path("fir_body_impl.tpp")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        _ensure_sources(config.root_dir)
        return {"kernel_sources": config.root_dir / "fir_body_impl.tpp"}


def _dump(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Python: worked examples, scenarios, the pure model, pysim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class WorkedExamplesStep(BuildStep):
    description = "Run the hand-computed worked examples against fir_eval."
    consumes = ["fir_source"]
    produces = {"worked_examples": Path("results/worked_examples.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        r = subprocess.run([sys.executable, str(_SOURCE_DIR / "worked_examples.py")],
                           capture_output=True, text=True)
        print(r.stdout[-600:])
        if r.returncode:
            raise RuntimeError("worked examples failed")
        return {"worked_examples": _SOURCE_DIR / "results" / "worked_examples.txt"}


@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response (scenarios.py)."
    consumes = ["fir_source", "scenarios_source", "worked_examples"]
    produces = {"data_dir": Path("data"), "scenario_list": Path("data/w32/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        data = config.root_dir / "data"
        S.write_scenarios(data)
        return {"data_dir": data, "scenario_list": data / "w32" / "scenarios.txt"}


def _names(root: Path, bw: int) -> list[str]:
    return (root / "data" / f"w{bw}" / "scenarios.txt").read_text(encoding="utf-8").split()


@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (fir_stream_model) over the whole session."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        for bw in WIDTHS:
            st = FirState()
            for name in _names(config.root_dir, bw):
                d = config.root_dir / "data" / f"w{bw}" / name
                out, st = fir_stream_model(read_bursts(d / "in"), bw, st)
                write_bursts(out, d / "model")
        done = config.root_dir / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("model\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body with its timing model, on the well-formed scenarios.

    Each scenario starts from the state the session has reached (``state_in.json``, written
    from the intents), since a pysim stream cannot carry the missing-TLAST scenario.
    """
    description = "Run pysim on the well-formed scenarios; log the timing scenario."
    consumes = ["scenario_list", "fir_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        for bw in WIDTHS:
            for name in S.WELL_FORMED:
                d = config.root_dir / "data" / f"w{bw}" / name
                sim = Simulation()
                clk = Clock(freq=clk_freq)
                log_path = config.root_dir / "results" / f"pysim_log_w{bw}.csv"
                extra = ({"logger": Logger(name="fir_log", sim=sim, file_path=log_path,
                                           fields=["event", "job"])}
                         if name == "timing" else {})
                accel = FirAccel(name="fir_accel", sim=sim, clk=clk, in_bw=bw, out_bw=bw,
                                 init_state=S.load_state(d / "state_in.json"), **extra)
                tb = FirTB(name="fir_tb", sim=sim, stimulus=d / "in", word_bw=bw,
                           n_out=len(read_bursts(d / "expected")))
                connect(sim, tb, accel, clk)
                sim.run_sim()
                write_bursts(tb.out, d / "pysim")
                if name == "timing":
                    _dump(config.root_dir / "results" / f"pysim_timing_w{bw}.json",
                          [{"job": j, "opcode": o, "count": c, "t_first_payload": t0,
                            "t_footer_end": t1} for j, o, c, t0, t1 in accel.timing])
        done = config.root_dir / "results" / "pysim_done.txt"
        done.write_text("\n".join(S.WELL_FORMED) + "\n", encoding="utf-8")
        return {"pysim_done": done}


@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    """pysim's cycles per PROCESS command of the timing scenario: first payload word read to
    the footer written -- the same span ``cosim_timing`` measures on the VCD."""
    description = "Extract the timing scenario's cycle counts from the pysim event logs."
    consumes = ["pysim_done"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        res = {}
        for bw in WIDTHS:
            rows = json.loads((config.root_dir / "results" / f"pysim_timing_w{bw}.json")
                              .read_text(encoding="utf-8"))
            by_job = {r["job"]: r for r in rows}
            for job, nsamp in TIMING_JOBS.items():
                r = by_job[job]
                res[f"w{bw}_n{nsamp}"] = int(round((r["t_footer_end"] - r["t_first_payload"])
                                                   * clk_freq))
        out = _dump(config.root_dir / "results" / "py_timing.json",
                    {"source": "py_sim", "span": "first payload word in -> last footer word out",
                     "transaction_cycles": res})
        return {"py_timing": out}


@dataclass(kw_only=True)
class CheckStep(BuildStep):
    """Compare one stage's recorded responses with the expected ones (scenarios.check)."""
    stage: str
    done_artifact: str
    only: tuple[str, ...] | None = None
    description = "Compare a stage's responses with the expected responses."
    params = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.done_artifact, "scenario_list"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"check_{self.stage}": Path(f"results/check_{self.stage}.json")}

    def run(self, config: BuildConfig, **_) -> dict:
        full, nfail = {}, 0
        for bw in WIDTHS:
            report = S.check(config.root_dir / "data" / f"w{bw}", self.stage, bw,
                             list(self.only) if self.only else None)
            full[f"w{bw}"] = report
            for name, problems in report.items():
                print(f"    {self.stage:6s} w{bw} {name:16s} {'PASS' if not problems else 'FAIL'}")
                for p in problems:
                    print(f"        {p}")
            nfail += sum(1 for v in report.values() if v)
        out = _dump(config.root_dir / "results" / f"check_{self.stage}.json", full)
        if nfail:
            raise RuntimeError(f"{self.stage}: {nfail} check(s) differ from the expected response")
        return {f"check_{self.stage}": out}


# ---------------------------------------------------------------------------
# Vitis: headers, the kernel boundary, csim, csynth + cosim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class HlsGenIncludeStep(BuildStep):
    description = "Generate the schema headers, serializers, and stream/testbench helpers."
    consumes = ["fir_source"]
    params = {}
    include_dir: str = "include"

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"include_dir": Path(self.include_dir)}

    def run(self, config: BuildConfig, **_) -> dict:
        inner = BuildDag()
        inner.add(StreamUtilsStep(output_dir=self.include_dir))
        for cls in SCHEMA_CLASSES:
            inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                     include_dir=self.include_dir))
        inner.add(ArrayUtilsStep(Int16, WORD_BW_SUPPORTED))
        failed = [n for n, r in inner.run(config).items() if not r.success]
        if failed:
            raise RuntimeError(f"header generation failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


def _run_vitis(config: BuildConfig, stage: str, bw: int, live_output: bool,
               clk_freq: float, trace: str = "none") -> None:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_FIR_STAGE": stage, "WAVEFLOW_FIR_BW": str(bw),
           "WAVEFLOW_FIR_TOP": TOPS[bw], "WAVEFLOW_FIR_TRACE_LEVEL": trace,
           "WAVEFLOW_FIR_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    log = config.root_dir / "results" / "logs" / f"vitis_{stage}_w{bw}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=True, env=env)
        log.write_text(result.stdout or "", encoding="utf-8")
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        log.write_text(out, encoding="utf-8")
        raise RuntimeError(f"Vitis {stage} w{bw} failed: {exc}\n{out[-3000:]}") from exc
    print((result.stdout or "")[-1500:])


def _stage_dirs(root: Path, stage: str) -> list[str]:
    names = []
    for bw in WIDTHS:
        for name in _names(root, bw):
            d = root / "data" / f"w{bw}" / name / stage
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
            names.append(name)
    return names


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Vitis C simulation of every scenario at both widths (fir_tb.cpp)."
    consumes = ["fir_cpp", "fir_hpp", "fir_body_impl", "include_dir", "scenario_list"]
    produces = {"csim_done": Path("results/csim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        _require_real_body(config.root_dir)
        _stage_dirs(config.root_dir, "csim")
        for bw in WIDTHS:
            _run_vitis(config, "csim", bw, live_output, clk_freq)
        done = config.root_dir / "results" / "csim_done.txt"
        done.write_text("csim\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    description = "C synthesis, then RTL co-simulation of every scenario, at both widths."
    consumes = ["fir_cpp", "fir_hpp", "fir_body_impl", "include_dir", "check_csim"]
    produces = {"cosim_done": Path("results/cosim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        _stage_dirs(config.root_dir, "cosim")
        for bw in WIDTHS:
            _run_vitis(config, "synth", bw, live_output, clk_freq, trace="port")
        done = config.root_dir / "results" / "cosim_done.txt"
        done.write_text("cosim\n", encoding="utf-8")
        return {"cosim_done": done}


@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    """Loops, II, estimated clock and resources from each width's synthesis report; the
    reports themselves are copied to results/reports/."""
    description = "Parse the C-synthesis reports: loop II, timing and resources."
    consumes = ["cosim_done"]
    produces = {"synth": Path("results/synth.json")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from waveflow.utils.csynthparse import CsynthParser

        res = {}
        rep_dir = config.root_dir / "results" / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        for bw in WIDTHS:
            sol = config.root_dir / f"prj_synth_w{bw}" / "solution1"
            parser = CsynthParser(sol_path=str(sol))
            parser.get_loop_pipeline_info()
            parser.get_resources()
            print(parser.loop_df.to_string() if not parser.loop_df.empty else "(no loops)")
            print(parser.res_df.to_string() if not parser.res_df.empty else "(no resources)")
            parser.loop_df.to_csv(config.root_dir / "results" / f"loop_df_w{bw}.csv", index=False)
            parser.res_df.to_csv(config.root_dir / "results" / f"res_df_w{bw}.csv", index=False)
            top = TOPS[bw]
            for src in [sol / "syn" / "report" / f"{top}_csynth.rpt",
                        sol / "syn" / "report" / "csynth.rpt",
                        sol / "sim" / "report" / f"{top}_cosim.rpt"]:
                if src.exists():
                    shutil.copy(src, rep_dir / f"w{bw}_{src.name}")
            res[f"w{bw}"] = _parse_csynth_xml(sol / "syn" / "report" / "csynth.xml")
        out = _dump(config.root_dir / "results" / "synth.json", res)
        return {"synth": out}


def _parse_csynth_xml(path: Path) -> dict:
    """Clock and resource numbers from Vitis' csynth.xml."""
    import xml.etree.ElementTree as ET

    r = ET.parse(path).getroot()

    def txt(p):
        e = r.find(p)
        return None if e is None else e.text

    out = {
        "part": txt("UserAssignments/Part"),
        "target_clock_ns": float(txt("UserAssignments/TargetClockPeriod")),
        "estimated_clock_ns": float(txt("PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod")),
    }
    for k in ("BRAM_18K", "DSP", "FF", "LUT", "URAM"):
        out[k] = int(txt(f"AreaEstimates/Resources/{k}") or 0)
        out[f"{k}_avail"] = int(txt(f"AreaEstimates/AvailableResources/{k}") or 0)
    return out


@dataclass(kw_only=True)
class VcdStep(BuildStep):
    description = "Export each width's cosim waveform (port trace) as a VCD."
    consumes = ["check_cosim"]
    produces = {"vcd_done": Path("results/vcd_done.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from waveflow.scripts.xsim_vcd import run_xsim_vcd

        for bw in WIDTHS:
            p = run_xsim_vcd(top=TOPS[bw], comp=f"prj_synth_w{bw}", soln="solution1",
                             out=f"fir_w{bw}.vcd", workdir=config.root_dir, trace_level="port")
            print(f"    VCD: {p}")
        done = config.root_dir / "results" / "vcd_done.txt"
        done.write_text("vcd\n", encoding="utf-8")
        return {"vcd_done": done}


@dataclass(kw_only=True)
class CosimTimingStep(BuildStep):
    description = "Latency, input rate and cycles per command from the VCDs; timing figure."
    consumes = ["vcd_done"]
    produces = {"cosim_timing": Path("results/cosim_timing.json")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from timing_analysis import analyze_session, plot_transaction

        res = {}
        for bw in WIDTHS:
            a = analyze_session(config.root_dir / "vcd" / f"fir_w{bw}.vcd",
                                config.root_dir / "data" / f"w{bw}", bw)
            res[f"w{bw}"] = a.summary()
            plot_transaction(a, "timing", 1,
                             config.root_dir / "results" / f"timing_process64_w{bw}.png")
        out = _dump(config.root_dir / "results" / "cosim_timing.json", res)
        return {"cosim_timing": out}


@dataclass(kw_only=True)
class ValidateTimingStep(BuildStep):
    """pysim's cycles per PROCESS command against the RTL's, tolerance 20 cycles."""
    description = "Compare pysim and cosim cycles per PROCESS command."
    consumes = ["py_timing", "cosim_timing"]
    produces = {"timing_verdict": Path("results/timing_verdict.json")}
    params = {}
    tolerance_cycles: int = 20

    def run(self, config: BuildConfig, py_timing, cosim_timing, **_) -> dict:
        py = json.loads(Path(py_timing).read_text())["transaction_cycles"]
        co = json.loads(Path(cosim_timing).read_text())
        rows, ok = {}, True
        for bw in WIDTHS:
            for job, nsamp in TIMING_JOBS.items():
                key = f"w{bw}_n{nsamp}"
                c = co[f"w{bw}"]["process"][str(nsamp)]["payload_to_footer_cycles"]
                d = abs(c - py[key])
                rows[key] = {"py_cycles": py[key], "cosim_cycles": c, "delta": d,
                             "pass": d <= self.tolerance_cycles}
                ok &= d <= self.tolerance_cycles
                print(f"    {key}: pysim {py[key]}  cosim {c}  delta {d}")
        out = _dump(config.root_dir / "results" / "timing_verdict.json",
                    {"pass": ok, "tolerance": self.tolerance_cycles, "rows": rows})
        if not ok:
            raise RuntimeError("pysim timing differs from cosim by more than the tolerance")
        return {"timing_verdict": out}


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check."""
    description = "Collect every check, the synthesis report and the timing verdict."
    consumes = ["check_model", "check_pysim", "check_csim", "check_cosim", "synth",
                "cosim_timing", "timing_verdict", "worked_examples"]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {w: {n: (not v) for n, v in r.items()}
                                                   for w, r in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "timing_verdict": load("timing_verdict"),
            "cosim_timing": load("cosim_timing"),
            "synth": load("synth"),
        }
        out = _dump(config.root_dir / "results" / "summary.json", summary)
        return {"summary": out}


def build_fir_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="fir_source", path=_SOURCE_DIR / "fir.py",
                       description="Schemas, the pure model, the module and the pysim testbench."))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py",
                       description="The scenarios, their expected responses, and the checker."))

    dag.add(WorkedExamplesStep(name="worked_examples"))
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifact="model_done"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifact="pysim_done",
                      only=S.WELL_FORMED))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=FirAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifact="csim_done"))
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(name="inspect_synth"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifact="cosim_done"))
    dag.add(VcdStep(name="vcd"))
    dag.add(CosimTimingStep(name="cosim_timing"))
    dag.add(ValidateTimingStep(name="validate_timing"))
    dag.add(SummaryStep(name="summary"))
    return dag


def main() -> None:
    run_dag_cli(
        build_fir_dag,
        description="Build the command-driven FIR accelerator.",
        default_through="check_pysim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--clk-freq",), {"type": float, "default": 100e6, "metavar": "HZ"}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {"clk_freq": a.clk_freq, "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
