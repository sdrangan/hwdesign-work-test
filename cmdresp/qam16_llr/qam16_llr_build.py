"""qam16_llr_build.py -- the build pipeline for the 16-QAM LLR demapper.

    python qam16_llr_build.py --list-steps
    python qam16_llr_build.py --through check_pysim       # Python only, no Vitis
    python qam16_llr_build.py --through summary           # everything, Vitis included

32-bit stream words only (frame F2).  The pipeline, in reading order:

    check_eval       qam16_llr_eval vs hand-computed examples and an independent reference
    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    checker_demo     the checker rejecting three constructed wrong outputs
    py_model         run the pure bit-exact model on every scenario
    check_model      ...and compare it with the expected response
    py_sim           run pysim (the timing model) on the scenarios a pysim stream can express
    check_pysim      ...compare
    extract_py_timing    pysim's cycle count for the timing scenario (rand_1024)
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place (a build outside this directory)
    gen_kernel       the kernel boundary (gen/qam16_llr.hpp, gen/qam16_llr.cpp); the body is
                     the hand-written qam16_llr_body_impl.tpp
    csim             Vitis C simulation, every scenario, hand-written qam16_llr_tb.cpp
    check_csim       ...compare
    csynth           C synthesis, then ONE RTL co-simulation run over every scenario
    inspect_synth    loops, II and resources from the synthesis report
    check_cosim      the co-simulated responses, compared
    cosim_timing     per-activation cosim latencies; the timing scenario vs pysim (20 cycles)
    summary          every check, the synthesis reports and the timing verdict

Every check compares against the same expected response, computed from each scenario's
intent (scenarios.py) rather than from any implementation.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import waveflow.build as waveflow_build
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.resource_steps import InspectSynthStep
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_bursts, write_bursts

import check_eval
import checker_demo
import scenarios as S
from qam16_llr import (
    ARRAY_ELEMS, SCHEMA_CLASSES, WORD_BW_SUPPORTED, Qam16LlrAccel, Qam16LlrTB, connect,
    qam16_llr_stream_model,
)

_SOURCE_DIR = Path(__file__).resolve().parent

W = 32
TOP = "qam16_llr"

#: The hand-written sources Vitis needs beside the generated ones.  A build in another
#: directory gets a copy.
HAND_WRITTEN = ("run.tcl", "qam16_llr_tb.cpp", "qam16_llr_body_impl.tpp")


def width_dir(root: Path) -> Path:
    """The scenario data: ``data/w32``."""
    return root / "data" / f"w{W}"


def _ensure_sources(root: Path) -> None:
    for name in HAND_WRITTEN:
        if not (root / name).exists():
            shutil.copy(_SOURCE_DIR / name, root / name)


_STUB_MARK = "TODO: implement body"


def _require_real_body(root: Path) -> None:
    """Refuse to simulate the generated stub (written only when the body is missing)."""
    body = root / "qam16_llr_body_impl.tpp"
    if _STUB_MARK in body.read_text(encoding="utf-8"):
        raise RuntimeError(f"{body} is the generated stub, not the kernel body: copy the "
                           f"hand-written qam16_llr_body_impl.tpp into the build directory")


@dataclass(kw_only=True)
class SourcesStep(BuildStep):
    """Place the hand-written C++ in the build directory, before anything is generated."""
    description = "Copy the hand-written body, testbench and Tcl into the build directory."
    consumes = ["qam16_llr_source"]
    produces = {"kernel_sources": Path("qam16_llr_body_impl.tpp")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        _ensure_sources(config.root_dir)
        return {"kernel_sources": config.root_dir / "qam16_llr_body_impl.tpp"}


def _write_status(path: Path, status: dict) -> None:
    path.write_text(json.dumps(status) + "\n", encoding="utf-8")


def _names(root: Path) -> list[str]:
    return (width_dir(root) / "scenarios.txt").read_text(encoding="utf-8").split()


def _meta(root: Path, name: str) -> dict:
    return json.loads((width_dir(root) / name / "scenario.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Python: the function, scenarios, the pure model, pysim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class CheckEvalStep(BuildStep):
    description = "qam16_llr_eval vs the hand-computed examples and an independent reference."
    consumes = ["qam16_llr_source"]
    produces = {"eval_check": Path("results/eval_check.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        if check_eval.main() != 0:
            raise RuntimeError("qam16_llr_eval disagrees with results/eval_check.txt's references")
        return {"eval_check": config.root_dir / "results" / "eval_check.txt"}


@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response."
    consumes = ["qam16_llr_source", "scenarios_source", "eval_check"]
    produces = {"scenario_list": Path("data/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        S.write_scenarios(width_dir(root), W)
        out = root / "data" / "scenarios.txt"
        out.write_text("\n".join(S.scenarios()) + "\n", encoding="utf-8")
        return {"scenario_list": out}


@dataclass(kw_only=True)
class CheckerDemoStep(BuildStep):
    description = "Show the checker rejecting three constructed wrong outputs."
    consumes = ["scenario_list"]
    produces = {"checker_demo": Path("results/checker_demo.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        if checker_demo.main() != 0:
            raise RuntimeError("the checker accepted a wrong output: results/checker_demo.txt")
        return {"checker_demo": config.root_dir / "results" / "checker_demo.txt"}


@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (qam16_llr_stream_model) on every scenario."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        for name in _names(root):
            d = width_dir(root) / name
            res = qam16_llr_stream_model(read_bursts(d / "in"), word_bw=W)
            write_bursts(res.out, d / "model")
            _write_status(d / "model" / "status.json", res.status())
        done = root / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body with its timing model, on every scenario a pysim stream
    can express (all but a missing TLAST).  The timing scenario's event log becomes the cycle
    estimate cosim is checked against."""
    description = "Run pysim on every scenario it can express; log the timing scenario."
    consumes = ["scenario_list", "qam16_llr_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt"),
                "log": Path(f"results/sim_log_w{W}.csv")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        root = config.root_dir
        log_path = root / "results" / f"sim_log_w{W}.csv"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for name in _names(root):
            if not _meta(root, name)["pysim"]:
                continue
            d = width_dir(root) / name
            sim = Simulation()
            clk = Clock(freq=clk_freq)
            logger = (Logger(name="qam16_llr_log", sim=sim, file_path=log_path,
                             fields=["event", "job"]) if name == S.TIMING_SCENARIO else None)
            accel = Qam16LlrAccel(name="qam16_llr_accel", sim=sim, clk=clk, in_bw=W, out_bw=W,
                                  **({"logger": logger} if logger else {}))
            tb = Qam16LlrTB(name="qam16_llr_tb", sim=sim, stimulus=d / "in", word_bw=W,
                            n_out=len(read_bursts(d / "expected")))
            connect(sim, tb, accel, clk)
            sim.run_sim()
            write_bursts(tb.out, d / "pysim")
            _write_status(d / "pysim" / "status.json", tb.status)
        done = root / "results" / "pysim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"pysim_done": done, "log": log_path}


@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    """pysim's cycle count for the timing scenario: one whole kernel call, ``ap_start`` to
    return -- the same span the cosim latency measures."""
    description = "Extract the timing scenario's cycle count from the pysim event log."
    consumes = ["log"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **art) -> dict:
        events: dict[str, float] = {}
        with open(art["log"], newline="") as f:
            for row in csv.DictReader(f):
                events.setdefault(row["event"], float(row["time"]))
        t0, t1 = events.get("proc_begin"), events.get("proc_end")
        if t0 is None or t1 is None:
            raise RuntimeError(f"missing timing events in {art['log']}: {sorted(events)}")
        out = config.root_dir / "results" / "py_timing.json"
        out.write_text(json.dumps({
            "scenario": S.TIMING_SCENARIO,
            "transaction_cycles": int(round((t1 - t0) * clk_freq)),
            "clk_freq": float(clk_freq), "source": "py_sim",
        }, indent=2), encoding="utf-8")
        return {"py_timing": out}


@dataclass(kw_only=True)
class CheckStep(BuildStep):
    """Compare one stage's recorded responses with the expected ones (scenarios.check).
    ``which`` names a scenario.json flag that selects the scenarios (None: all)."""
    stage: str
    done_artifacts: tuple[str, ...]
    which: str | None = None
    description = "Compare a stage's responses with the expected responses."
    params = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [*self.done_artifacts, "scenario_list"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"check_{self.stage}": Path(f"results/check_{self.stage}.json")}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        names = [n for n in _names(root) if self.which is None or _meta(root, n)[self.which]]
        report = {f"w{W}/{name}": problems
                  for name, problems in S.check(width_dir(root), self.stage, names).items()}
        out = root / "results" / f"check_{self.stage}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        failed = {k: v for k, v in report.items() if v}
        for name, problems in report.items():
            print(f"    {self.stage:6s} {name:24s} {'PASS' if not problems else 'FAIL'}")
            for p in problems:
                print(f"        {p}")
        if failed:
            raise RuntimeError(f"{self.stage}: {len(failed)} scenario(s) differ from the "
                               f"expected response: {sorted(failed)}")
        return {f"check_{self.stage}": out}


# ---------------------------------------------------------------------------
# Vitis: headers, the kernel boundary, csim, csynth + cosim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class HlsGenIncludeStep(BuildStep):
    description = "Generate the schema headers, serializers, and stream/testbench helpers."
    consumes = ["qam16_llr_source"]
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
        for elem in ARRAY_ELEMS:
            inner.add(ArrayUtilsStep(elem, WORD_BW_SUPPORTED))
        failed = [n for n, r in inner.run(config).items() if not r.success]
        if failed:
            raise RuntimeError(f"header generation failed: {failed}")
        # The complex int16 element's helpers include Waveflow's shipped wf_cint.h; copied
        # beside them, as examples/schemas/complex does.
        shutil.copy(Path(waveflow_build.__file__).resolve().parent / "wf_cint.h",
                    config.root_dir / self.include_dir / "wf_cint.h")
        return {"include_dir": config.root_dir / self.include_dir}


def _run_vitis(config: BuildConfig, stage: str, live_output: bool, clk_freq: float) -> None:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_POLY_STAGE": stage, "WAVEFLOW_POLY_WIDTH": str(W),
           "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=not live_output, env=env)
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"Vitis {stage} failed: {exc}\n{out[-3000:]}") from exc
    if not live_output and result.stdout:
        print(result.stdout[-2000:])


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Vitis C simulation of every scenario, with qam16_llr_tb.cpp."
    consumes = ["qam16_llr_cpp", "qam16_llr_hpp", "qam16_llr_body_impl", "include_dir", "scenario_list"]
    produces = {"csim_done": Path("results/csim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root = config.root_dir
        _require_real_body(root)
        for name in _names(root):
            (width_dir(root) / name / "csim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "csim", live_output, clk_freq)
        done = root / "results" / "csim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """C synthesis, then one RTL co-simulation run whose testbench walks every scenario."""
    description = "Vitis C synthesis, then one RTL co-simulation run over every scenario."
    params = {"live_output": False, "clk_freq": 100e6}
    consumes = ["qam16_llr_cpp", "qam16_llr_hpp", "qam16_llr_body_impl", "include_dir", "check_csim"]
    produces = {"report_dir": Path(f"w{W}_proj/solution1"),
                "cosim_done": Path("results/cosim_done.txt")}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root = config.root_dir
        for name in _names(root):
            (width_dir(root) / name / "cosim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "synth", live_output, clk_freq)
        done = root / "results" / "cosim_done.txt"
        done.write_text("all scenarios\n", encoding="utf-8")
        return {"report_dir": root / f"w{W}_proj" / "solution1", "cosim_done": done}


def _cosim_latencies(sol: Path) -> list[int]:
    """Each kernel call's latency, in call order, from the cosim transaction trace.

    Vitis 2025.1 writes one row per call (``transaction  <k>:  <latency>  <interval>``) to
    ``sim/report/verilog/result.transaction.rpt``.
    """
    rpt = sol / "sim" / "report" / "verilog" / "result.transaction.rpt"
    if rpt.exists():
        lats = [int(m) for m in re.findall(r"transaction\s+\d+:\s+(\d+)",
                                            rpt.read_text(encoding="utf-8"))]
        if lats:
            return lats
    raise RuntimeError(f"no per-transaction cosim latency found under {sol / 'sim'}")


@dataclass(kw_only=True)
class CosimTimingStep(BuildStep):
    """Per-activation cosim latencies, and the timing scenario against pysim (20 cycles)."""
    description = "Per-activation cosim latency; timing scenario vs pysim within 20 cycles."
    consumes = ["report_dir", "py_timing", "scenario_list"]
    produces = {"cosim_timing": Path("results/cosim_timing.json")}
    params = {}
    tolerance_cycles: int = 20

    def run(self, config: BuildConfig, **art) -> dict:
        root = config.root_dir
        names = _names(root)
        lats = _cosim_latencies(Path(art["report_dir"]))
        if len(lats) != len(names):
            raise RuntimeError(f"{len(lats)} cosim transactions for {len(names)} scenarios")
        per = dict(zip(names, lats))
        py = json.loads(Path(art["py_timing"]).read_text(encoding="utf-8"))
        cyc = per[S.TIMING_SCENARIO]
        delta = abs(cyc - int(py["transaction_cycles"]))
        verdict = {
            "source": str(Path(art["report_dir"]) / "sim" / "report" / "verilog"
                          / "result.transaction.rpt"),
            "per_activation_latency": per,
            "timing_scenario": S.TIMING_SCENARIO,
            "cosim_cycles": cyc,
            "py_cycles": int(py["transaction_cycles"]),
            "delta": delta,
            "tolerance": self.tolerance_cycles,
            "pass": delta <= self.tolerance_cycles,
        }
        out = root / "results" / "cosim_timing.json"
        out.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        if not verdict["pass"]:
            raise RuntimeError(f"pysim timing off by {delta} cycles (> {self.tolerance_cycles}): {out}")
        return {"cosim_timing": out}


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check."""
    description = "Collect every check, the synthesis reports and the timing verdict."
    consumes = ["eval_check", "checker_demo", "check_model", "check_pysim", "check_csim",
                "check_cosim", "loop_df", "resources", "cosim_timing"]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {n: (not v) for n, v in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "timing": load("cosim_timing"),
            "loops": Path(art["loop_df"]).read_text(encoding="utf-8"),
            "resources": load("resources"),
        }
        out = config.root_dir / "results" / "summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return {"summary": out}


def build_qam16_llr_dag() -> BuildDag:
    """The whole pipeline.  Parameters come from ``BuildConfig.params``: ``clk_freq`` and
    ``live_output``."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="qam16_llr_source", path=_SOURCE_DIR / "qam16_llr.py",
                       description="Schemas, the pure model, the module and the pysim testbench."))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py",
                       description="The scenarios, their expected responses, and the checker."))

    # Python: the function, the model and pysim, checked against the expected responses.
    dag.add(CheckEvalStep(name="check_eval"))
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(CheckerDemoStep(name="checker_demo"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifacts=("model_done",)))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifacts=("pysim_done",),
                      which="pysim"))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    # Code generation: headers, and the kernel boundary around the hand-written body.
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=Qam16LlrAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    # Vitis: csim on every scenario, then synthesis and one cosim run over every scenario.
    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifacts=("csim_done",)))
    dag.add(CSynthStep(name="csynth"))
    # The framework's step: the report attributed, the loop table written, and the build
    # failed on a pipelined loop at II > 1.
    dag.add(InspectSynthStep(
        name="inspect_synth", comp_class=Qam16LlrAccel, top_name=TOP,
        elaborate_params=("in_bw", "out_bw"), params={"in_bw": W, "out_bw": W},
        report_artifact="report_dir", seconds_artifact=None,
        output=Path("results/resources.json"), output_artifact="resources",
        require_ii1=True, loop_csv=Path("results/loop_df.csv"), loop_artifact="loop_df"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifacts=("cosim_done",)))
    dag.add(CosimTimingStep(name="cosim_timing"))
    dag.add(SummaryStep(name="summary"))
    return dag


def main() -> None:
    run_dag_cli(
        build_qam16_llr_dag,
        description="Build the 16-QAM LLR demapper.",
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
