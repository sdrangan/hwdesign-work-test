"""maxpool_build.py -- the build pipeline for the streaming 1-D max-pool accelerator.

    python maxpool_build.py --list-steps
    python maxpool_build.py --through check_pysim       # Python only, no Vitis
    python maxpool_build.py --through summary           # everything, Vitis included

One stream width, 32 bits (two int16 samples per word).  The pipeline, in reading order:

    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    check_eval       maxpool_eval vs hand examples and the NumPy reference; checker rejections
    py_model         run the pure bit-exact model on every scenario
    check_model      ...and compare it with the expected response
    py_sim           run pysim (the timing model) on the well-formed scenarios
    check_pysim      ...compare
    extract_py_timing   pysim's cycle count for the timing scenario
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place
    gen_kernel       the kernel boundary (gen/maxpool.hpp, gen/maxpool.cpp)
    csim             Vitis C simulation, every scenario, hand-written maxpool_tb.cpp
    check_csim       ...compare
    csynth           C synthesis, then ONE RTL co-simulation walking every cosim scenario
    inspect_synth    loops, II and resources from the synthesis report
    check_cosim      the co-simulated responses, compared
    cosim_timing     per-activation cycle counts from the cosim run
    validate_timing  cosim cycles of the timing scenario vs the pysim estimate (20 cycles)
    summary          every check, the synthesis report and the timing verdict

Every check compares against the same expected response, computed from each scenario's
intent (scenarios.py) rather than from any implementation.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.cosim_steps import ValidateTimingStep
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

_SOURCE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SOURCE_DIR))

import scenarios as S  # noqa: E402
from maxpool import (  # noqa: E402
    SCHEMA_CLASSES, WORD_BW, Int16, MaxpoolAccel, MaxpoolTB, connect, maxpool_stream_model,
)

TOP = "maxpool"

#: The hand-written sources Vitis needs beside the generated ones.
HAND_WRITTEN = ("run.tcl", "maxpool_tb.cpp", "maxpool_body_impl.tpp")


def data_dir(root: Path) -> Path:
    """The scenario data: ``data/w32``."""
    return root / "data" / f"w{WORD_BW}"


def _ensure_sources(root: Path) -> None:
    for name in HAND_WRITTEN:
        if not (root / name).exists():
            shutil.copy(_SOURCE_DIR / name, root / name)


_STUB_MARK = "TODO: implement body"


def _require_real_body(root: Path) -> None:
    body = root / "maxpool_body_impl.tpp"
    if _STUB_MARK in body.read_text(encoding="utf-8"):
        raise RuntimeError(f"{body} is the generated stub, not the kernel body")


@dataclass(kw_only=True)
class SourcesStep(BuildStep):
    """Place the hand-written C++ in the build directory, before anything is generated."""
    description = "Copy the hand-written body, testbench and Tcl into the build directory."
    consumes = ["maxpool_source"]
    produces = {"kernel_sources": Path("maxpool_body_impl.tpp")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        _ensure_sources(config.root_dir)
        return {"kernel_sources": config.root_dir / "maxpool_body_impl.tpp"}


def _write_status(path: Path, status: dict) -> None:
    path.write_text(json.dumps(status) + "\n", encoding="utf-8")


def _names(root: Path, which: str = "scenarios.txt") -> list[str]:
    return (data_dir(root) / which).read_text(encoding="utf-8").split()


def _meta(root: Path, name: str) -> dict:
    return json.loads((data_dir(root) / name / "scenario.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Python: scenarios, the pure model, pysim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response."
    consumes = ["maxpool_source", "scenarios_source"]
    produces = {"scenario_list": Path("data/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        S.write_scenarios(data_dir(root))
        out = root / "data" / "scenarios.txt"
        out.write_text("\n".join(S.scenarios()) + "\n", encoding="utf-8")
        return {"scenario_list": out}


@dataclass(kw_only=True)
class CheckEvalStep(BuildStep):
    """maxpool_eval vs the hand-computed examples and the NumPy reference; the checker
    rejecting wrong outputs.  The script's output is kept in results/check_eval.txt."""
    description = "Run test_eval.py: hand examples, NumPy reference, checker rejections."
    consumes = ["scenario_list"]
    produces = {"check_eval": Path("results/check_eval.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        r = subprocess.run([sys.executable, str(_SOURCE_DIR / "test_eval.py"), str(data_dir(root))],
                           capture_output=True, text=True, cwd=_SOURCE_DIR)
        out = root / "results" / "check_eval.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(r.stdout + r.stderr, encoding="utf-8")
        print(r.stdout[-3000:])
        if r.returncode:
            raise RuntimeError(f"test_eval.py failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
        return {"check_eval": out}


@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (maxpool_stream_model) on every scenario."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        for name in _names(root):
            d = data_dir(root) / name
            res = maxpool_stream_model(read_bursts(d / "in"))
            write_bursts(res.out, d / "model")
            _write_status(d / "model" / "status.json", res.status())
        done = root / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body with its timing model, on every well-formed scenario.
    The timing scenario's event log becomes the cycle estimate cosim is checked against."""
    description = "Run pysim on every well-formed scenario; log the timing scenario."
    consumes = ["scenario_list", "maxpool_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt"),
                "log": Path("results/sim_log.csv")}
    params = {"clk_freq": 100e6, "unroll_factor": 1}

    def run(self, config: BuildConfig, clk_freq, unroll_factor, **_) -> dict:
        root = config.root_dir
        log_path = root / "results" / "sim_log.csv"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for name in _names(root):
            if not _meta(root, name)["pysim"]:
                continue
            d = data_dir(root) / name
            sim = Simulation()
            clk = Clock(freq=clk_freq)
            logger = (Logger(name="maxpool_log", sim=sim, file_path=log_path,
                             fields=["event", "job"]) if name == "timing" else None)
            accel = MaxpoolAccel(name="maxpool_accel", sim=sim, clk=clk,
                                 unroll_factor=unroll_factor,
                                 **({"logger": logger} if logger else {}))
            tb = MaxpoolTB(name="maxpool_tb", sim=sim, stimulus=d / "in",
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
    """pysim's cycle count for the timing scenario: one whole kernel call, start to return."""
    description = "Extract the timing scenario's cycle count from the pysim event log."
    consumes = ["log"]
    produces = {"py_timing": Path("results/py_timing.json")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **art) -> dict:
        log = art["log"]
        events: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                events.setdefault(row["event"], float(row["time"]))
        t0, t1 = events.get("proc_begin"), events.get("proc_end")
        if t0 is None or t1 is None:
            raise RuntimeError(f"missing timing events in {log}: {sorted(events)}")
        out = config.root_dir / "results" / "py_timing.json"
        out.write_text(json.dumps({
            "transaction_cycles": int(round((t1 - t0) * clk_freq)),
            "transaction_seconds": t1 - t0, "clk_freq": float(clk_freq),
            "word_bw": WORD_BW, "source": "py_sim",
            "events": {"proc_begin": t0, "proc_end": t1},
        }, indent=2), encoding="utf-8")
        return {"py_timing": out}


@dataclass(kw_only=True)
class CheckStep(BuildStep):
    """Compare one stage's recorded responses with the expected ones (scenarios.check)."""
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
        report = S.check(data_dir(root), self.stage, names)
        out = root / "results" / f"check_{self.stage}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        failed = {k: v for k, v in report.items() if v}
        for name, problems in report.items():
            print(f"    {self.stage:6s} {name:28s} {'PASS' if not problems else 'FAIL'}")
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
    consumes = ["maxpool_source"]
    params = {}
    include_dir: str = "include"

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"include_dir": Path(self.include_dir)}

    def run(self, config: BuildConfig, **_) -> dict:
        inner = BuildDag()
        inner.add(StreamUtilsStep(output_dir=self.include_dir))
        for cls in SCHEMA_CLASSES:
            inner.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW],
                                     include_dir=self.include_dir))
        inner.add(ArrayUtilsStep(Int16, [WORD_BW]))
        failed = [n for n, r in inner.run(config).items() if not r.success]
        if failed:
            raise RuntimeError(f"header generation failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


def _run_vitis(config: BuildConfig, stage: str, live_output: bool, clk_freq: float) -> None:
    _ensure_sources(config.root_dir)
    env = {"WAVEFLOW_POLY_STAGE": stage, "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
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
    description = "Vitis C simulation of every scenario, with maxpool_tb.cpp."
    consumes = ["maxpool_cpp", "maxpool_hpp", "maxpool_body_impl", "include_dir", "scenario_list"]
    produces = {"csim_done": Path("results/csim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root = config.root_dir
        _require_real_body(root)
        for name in _names(root):
            d = data_dir(root) / name / "csim"
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "csim", live_output, clk_freq)
        done = root / "results" / "csim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """C synthesis, then ONE RTL co-simulation: the testbench walks every cosim scenario in
    order, one kernel call per activation, with fresh streams."""
    description = "Vitis C synthesis, then one RTL co-simulation of every cosim scenario."
    consumes = ["maxpool_cpp", "maxpool_hpp", "maxpool_body_impl", "include_dir", "check_csim"]
    produces = {"report_dir": Path("w32_proj/solution1"),
                "cosim_done": Path("results/cosim_done.txt")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root = config.root_dir
        for name in _names(root, "scenarios_cosim.txt"):
            d = data_dir(root) / name / "cosim"
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "synth", live_output, clk_freq)
        done = root / "results" / "cosim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"report_dir": root / "w32_proj" / "solution1", "cosim_done": done}


@dataclass(kw_only=True)
class CosimTimingStep(BuildStep):
    """Per-activation cycle counts from the one cosim run.

    Vitis records every kernel call of the co-simulation as a transaction, in testbench
    order -- the order of ``scenarios_cosim.txt``.  This step reads each transaction's
    latency, names it by its scenario, and writes the timing scenario's count in the form
    ``ValidateTimingStep`` reads, plus the cycles per input word measured from the
    difference between ``tput_n128`` and ``tput_n64`` (32 more input words).
    """
    description = "Per-activation cosim latencies; the timing scenario; cycles per input word."
    consumes = ["report_dir", "cosim_done"]
    produces = {"cosim_timing": Path("results/cosim_timing.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        root = config.root_dir
        sol = Path(art["report_dir"])
        names = _names(root, "scenarios_cosim.txt")
        lat = _transaction_latencies(sol)
        if len(lat) != len(names):
            raise RuntimeError(f"{len(lat)} cosim transactions for {len(names)} scenarios")
        per = dict(zip(names, lat))
        words = (per["tput_n128"] - per["tput_n64"]) / 32.0
        out = root / "results" / "cosim_timing.json"
        out.write_text(json.dumps({
            "transaction_cycles": per["timing"], "source": "cosim", "scenario": "timing",
            "cycles_per_input_word": words, "per_scenario": per,
        }, indent=2), encoding="utf-8")
        return {"cosim_timing": out}


def _transaction_latencies(sol: Path) -> list[int]:
    """Every transaction's latency, in order, from the cosim transaction report
    (``sim/report/verilog/result.transaction.rpt``: ``transaction <i>: <latency> <interval>``)."""
    p = sol / "sim" / "report" / "verilog" / "result.transaction.rpt"
    if not p.exists():
        raise RuntimeError(f"no cosim transaction report at {p}")
    # The last transaction's interval is "x": nothing follows it.
    rows = re.findall(r"transaction\s+(\d+):\s+(\d+)\s+(\S+)", p.read_text(encoding="utf-8"))
    return [int(lat) for _, lat, _ in sorted(rows, key=lambda r: int(r[0]))]


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check."""
    description = "Collect every check, the synthesis report and the timing verdict."
    consumes = ["check_eval", "check_model", "check_pysim", "check_csim", "check_cosim",
                "loop_df", "resources", "timing_verdict", "cosim_timing"]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {n: (not v) for n, v in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "timing": load("timing_verdict"),
            "cosim_timing": load("cosim_timing"),
            "loops": Path(art["loop_df"]).read_text(encoding="utf-8"),
            "resources": load("resources"),
        }
        out = config.root_dir / "results" / "summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return {"summary": out}


def build_maxpool_dag() -> BuildDag:
    """The whole pipeline.  Parameters: ``clk_freq``, ``unroll_factor``, ``live_output``."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="maxpool_source", path=_SOURCE_DIR / "maxpool.py",
                       description="Schemas, the pure model, the module and the pysim testbench."))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py",
                       description="The scenarios, their expected responses, and the checker."))

    dag.add(ScenariosStep(name="scenarios"))
    dag.add(CheckEvalStep(name="check_eval"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifacts=("model_done",)))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifacts=("pysim_done",),
                      which="pysim"))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=MaxpoolAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifacts=("csim_done",)))
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(
        name="inspect_synth", comp_class=MaxpoolAccel, top_name=TOP,
        elaborate_params=("in_bw", "out_bw"), params={"in_bw": WORD_BW, "out_bw": WORD_BW},
        report_artifact="report_dir", seconds_artifact=None,
        output=Path("results/resources.json"), output_artifact="resources",
        require_ii1=True, loop_csv=Path("results/loop_df.csv"), loop_artifact="loop_df"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifacts=("cosim_done",),
                      which="cosim"))
    dag.add(CosimTimingStep(name="cosim_timing"))
    dag.add(ValidateTimingStep(
        name="validate_timing", py_timing_artifact="py_timing",
        cosim_timing_artifact="cosim_timing", tolerance_cycles=20,
        output_path="results/timing_verdict.json", verdict_artifact="timing_verdict"))
    dag.add(SummaryStep(name="summary"))
    return dag


def main() -> None:
    run_dag_cli(
        build_maxpool_dag,
        description="Build the streaming 1-D max-pool accelerator.",
        default_through="check_pysim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--clk-freq",), {"type": float, "default": 100e6, "metavar": "HZ"}),
            (("--unroll-factor",), {"type": int, "default": 1}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {"clk_freq": a.clk_freq, "unroll_factor": a.unroll_factor,
                                    "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
