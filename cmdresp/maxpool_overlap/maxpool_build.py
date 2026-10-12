"""maxpool_build.py -- the build pipeline for the streaming 2-D max-pool accelerator.

    python maxpool_build.py --list-steps
    python maxpool_build.py --through check_pysim       # Python only, no Vitis
    python maxpool_build.py --through summary           # everything, Vitis included

The pipeline, in reading order (32-bit stream words only):

    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    py_model         run the pure bit-exact model on every scenario
    check_model      ...and compare it with the expected response
    py_sim           run pysim (the timing model) on the scenarios a pysim stream can express
    check_pysim      ...compare
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place (a build outside this directory)
    gen_kernel       the kernel boundary (gen/maxpool.hpp, gen/maxpool.cpp); the body is the
                     hand-written maxpool_body_impl.tpp
    csim             Vitis C simulation, every scenario, hand-written maxpool_tb.cpp
    check_csim       ...compare
    csynth           C synthesis, then ONE RTL co-simulation that walks every scenario in order
    inspect_synth    loops, II and resources from the synthesis report
    check_cosim      every co-simulated response, compared
    cosim_tx         per-activation cycle counts from the cosim run
    extract_py_timing_<s> / validate_timing_<s>   pysim vs cosim cycles, per timing scenario
    throughput       pixels per clock while an image streams, from the cosim cycle counts
    summary          every check, the synthesis report and the timing verdicts

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

import scenarios as S
from maxpool import (
    SCHEMA_CLASSES, WORD_BW_SUPPORTED, Int16, MaxpoolAccel, MaxpoolTB, connect,
    maxpool_stream_model,
)

_SOURCE_DIR = Path(__file__).resolve().parent
W = 32
TOP = "maxpool"

#: The hand-written sources Vitis needs beside the generated ones.
HAND_WRITTEN = ("run.tcl", "maxpool_tb.cpp", "maxpool_body_impl.tpp")

#: The scenarios whose cycle count pysim must predict (scenarios.py, ``timing=True``).
TIMING = tuple(n for n, sc in S.scenarios().items() if sc.timing)


def data_dir(root: Path) -> Path:
    return root / "data" / f"w{W}"


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


def _names(root: Path) -> list[str]:
    return (data_dir(root) / "scenarios.txt").read_text(encoding="utf-8").split()


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
        names = S.write_scenarios(data_dir(root))
        out = root / "data" / "scenarios.txt"
        out.write_text("\n".join(names) + "\n", encoding="utf-8")
        return {"scenario_list": out}


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
            res = maxpool_stream_model(read_bursts(d / "in"), word_bw=W)
            write_bursts(res.out, d / "model")
            _write_status(d / "model" / "status.json", res.status())
        done = root / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body with its timing model, on every scenario a pysim
    stream can express (all but a missing TLAST).  Every run's event log is kept, so each
    activation's cycle count can be set beside the cosim one."""
    description = "Run pysim on every scenario it can express; log each activation."
    consumes = ["scenario_list", "maxpool_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        root = config.root_dir
        logs = root / "results" / "pysim_logs"
        logs.mkdir(parents=True, exist_ok=True)
        cycles: dict[str, int] = {}
        for name in _names(root):
            if not _meta(root, name)["pysim"]:
                continue
            d = data_dir(root) / name
            sim = Simulation()
            clk = Clock(freq=clk_freq)
            log_path = logs / f"{name}.csv"
            logger = Logger(name="maxpool_log", sim=sim, file_path=log_path,
                            fields=["event", "job"])
            accel = MaxpoolAccel(name="maxpool_accel", sim=sim, clk=clk, logger=logger)
            tb = MaxpoolTB(name="maxpool_tb", sim=sim, stimulus=d / "in", word_bw=W,
                           n_out=len(read_bursts(d / "expected")))
            connect(sim, tb, accel, clk)
            sim.run_sim()
            write_bursts(tb.out, d / "pysim")
            _write_status(d / "pysim" / "status.json", tb.status)
            cycles[name] = _py_cycles(log_path, clk_freq)
        (root / "results" / "pysim_cycles.json").write_text(json.dumps(cycles, indent=2),
                                                            encoding="utf-8")
        done = root / "results" / "pysim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"pysim_done": done}


def _py_cycles(log: Path, clk_freq: float) -> int:
    events: dict[str, float] = {}
    with open(log, newline="") as f:
        for row in csv.DictReader(f):
            events.setdefault(row["event"], float(row["time"]))
    t0, t1 = events.get("proc_begin"), events.get("proc_end")
    if t0 is None or t1 is None:
        raise RuntimeError(f"missing timing events in {log}: {sorted(events)}")
    return int(round((t1 - t0) * clk_freq))


@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    """pysim's cycle count for one timing scenario: one whole kernel call, ``ap_start`` to
    return -- the same span the cosim measures for that activation."""
    scenario: str
    description = "pysim's cycle count for one timing scenario."
    consumes = ["pysim_done"]
    params = {"clk_freq": 100e6}

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"py_timing_{self.scenario}": Path(f"results/py_timing_{self.scenario}.json")}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        root = config.root_dir
        cyc = json.loads((root / "results" / "pysim_cycles.json").read_text())[self.scenario]
        out = root / "results" / f"py_timing_{self.scenario}.json"
        out.write_text(json.dumps({"transaction_cycles": cyc, "clk_freq": float(clk_freq),
                                   "scenario": self.scenario, "source": "py_sim"}, indent=2),
                       encoding="utf-8")
        return {f"py_timing_{self.scenario}": out}


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
        report = {f"w{W}/{n}": p for n, p in S.check(data_dir(root), self.stage, names).items()}
        out = root / "results" / f"check_{self.stage}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        failed = {k: v for k, v in report.items() if v}
        for name, problems in report.items():
            print(f"    {self.stage:6s} {name:34s} {'PASS' if not problems else 'FAIL'}")
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
            inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                     include_dir=self.include_dir))
        inner.add(ArrayUtilsStep(Int16, WORD_BW_SUPPORTED))
        failed = [n for n, r in inner.run(config).items() if not r.success]
        if failed:
            raise RuntimeError(f"header generation failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


def _run_vitis(config: BuildConfig, stage: str, live_output: bool, clk_freq: float) -> str:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_MAXPOOL_STAGE": stage,
           "WAVEFLOW_MAXPOOL_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=not live_output, env=env)
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"Vitis {stage} failed: {exc}\n{out[-3000:]}") from exc
    out = (result.stdout or "") if not live_output else ""
    if out:
        print(out[-2000:])
    return out


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
            (data_dir(root) / name / "csim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "csim", live_output, clk_freq)
        done = root / "results" / "csim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """C synthesis, then ONE RTL co-simulation: the testbench walks every scenario in
    order, one kernel call per activation on fresh streams, error activations included."""
    description = "Vitis C synthesis, then one RTL co-simulation of every scenario."
    params = {"live_output": False, "clk_freq": 100e6}
    consumes = ["maxpool_cpp", "maxpool_hpp", "maxpool_body_impl", "include_dir", "check_csim"]
    produces = {"report_dir": Path("w32_proj/solution1"),
                "cosim_done": Path("results/cosim_done.txt")}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root = config.root_dir
        for name in _names(root):
            (data_dir(root) / name / "cosim").mkdir(parents=True, exist_ok=True)
        out = _run_vitis(config, "synth", live_output, clk_freq)
        (root / "results" / "vitis_synth_cosim.log").write_text(out, encoding="utf-8")
        done = root / "results" / "cosim_done.txt"
        done.write_text("all\n", encoding="utf-8")
        return {"report_dir": root / "w32_proj" / "solution1", "cosim_done": done}


def cosim_transaction_cycles(sol: Path) -> list[int]:
    """Every kernel call's latency in the cosim run, in call order: Vitis writes one
    ``transaction <k>: <latency> ...`` row per call to the RTL simulation's performance
    report."""
    rpt = sol / "sim" / "verilog" / f"{TOP}.performance.result.transaction.xml"
    if not rpt.exists():
        raise FileNotFoundError(f"no per-transaction cosim report at {rpt}")
    rows = re.findall(r"^transaction\s+(\d+):\s+(\d+)", rpt.read_text(), flags=re.M)
    return [int(lat) for _, lat in sorted(rows, key=lambda r: int(r[0]))]


@dataclass(kw_only=True)
class CosimTxStep(BuildStep):
    """Each activation's cycle count from the one cosim run, by scenario name."""
    description = "Per-activation cycle counts from the cosim run."
    consumes = ["cosim_done", "report_dir", "scenario_list"]
    produces = {"cosim_tx": Path("results/cosim_cycles.json")}
    params = {}

    def run(self, config: BuildConfig, report_dir, **_) -> dict:
        root = config.root_dir
        names = _names(root)
        lat = cosim_transaction_cycles(Path(report_dir))
        if len(lat) != len(names):
            raise RuntimeError(f"{len(lat)} cosim transactions for {len(names)} scenarios")
        out = root / "results" / "cosim_cycles.json"
        out.write_text(json.dumps(dict(zip(names, lat)), indent=2), encoding="utf-8")
        for s in TIMING:
            (root / "results" / f"cosim_timing_{s}.json").write_text(json.dumps(
                {"transaction_cycles": dict(zip(names, lat))[s], "scenario": s,
                 "source": "cosim"}, indent=2), encoding="utf-8")
        return {"cosim_tx": out}


@dataclass(kw_only=True)
class CosimTimingSplitStep(BuildStep):
    """Name one timing scenario's cosim count as its own artifact, for ValidateTimingStep."""
    scenario: str
    description = "One timing scenario's cosim cycle count."
    consumes = ["cosim_tx"]
    params = {}

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"cosim_timing_{self.scenario}": Path(f"results/cosim_timing_{self.scenario}.json")}

    def run(self, config: BuildConfig, **_) -> dict:
        return {f"cosim_timing_{self.scenario}":
                config.root_dir / "results" / f"cosim_timing_{self.scenario}.json"}


@dataclass(kw_only=True)
class ThroughputStep(BuildStep):
    """Pixels per clock while an image streams, from the cosim cycle counts.

    The two timing scenarios differ only in image height (64 x 256 and 256 x 256 pixels,
    same geometry otherwise), so the difference of their cycle counts over the difference
    of their pixel counts is the clocks each extra pixel costs, with every fixed overhead
    (start, header, pipeline fill, drain) cancelled.  The target is one pixel per clock.
    """
    description = "Clocks per input pixel while an image streams (cosim)."
    consumes = ["cosim_tx", "scenario_list"]
    produces = {"throughput": Path("results/throughput.json")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        cyc = json.loads((root / "results" / "cosim_cycles.json").read_text())
        npix = {s: sum(_meta(root, s)["npix"]) for s in TIMING}
        a, b = sorted(TIMING, key=lambda s: npix[s])
        cpp = (cyc[b] - cyc[a]) / (npix[b] - npix[a])
        res = {"scenarios": {s: {"pixels": npix[s], "cosim_cycles": cyc[s],
                                 "overhead_cycles": cyc[s] - npix[s]} for s in TIMING},
               "clocks_per_pixel": cpp, "pixels_per_clock": 1.0 / cpp,
               "required_pixels_per_clock": 1.0, "pass": cpp <= 1.0}
        out = root / "results" / "throughput.json"
        out.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"    throughput: {1.0 / cpp:.4f} pixels/clock "
              f"({cyc[b]} - {cyc[a]} cycles over {npix[b]} - {npix[a]} pixels)")
        if not res["pass"]:
            raise RuntimeError(f"throughput {1.0 / cpp:.4f} pixels/clock < 1")
        return {"throughput": out}


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check."""
    description = "Collect every check, the synthesis report and the timing verdicts."
    consumes = ["check_model", "check_pysim", "check_csim", "check_cosim", "loop_df",
                "resources", "throughput", *[f"timing_verdict_{s}" for s in TIMING]]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {n: (not v) for n, v in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "timing": {s: load(f"timing_verdict_{s}") for s in TIMING},
            "throughput": load("throughput"),
            "loops": Path(art["loop_df"]).read_text(encoding="utf-8"),
            "resources": load("resources"),
        }
        out = config.root_dir / "results" / "summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return {"summary": out}


def build_maxpool_dag() -> BuildDag:
    """The whole pipeline.  Parameters come from ``BuildConfig.params``: ``clk_freq`` and
    ``live_output``."""
    dag = BuildDag()
    dag.add(SourceStep(artifact="maxpool_source", path=_SOURCE_DIR / "maxpool.py",
                       description="Schemas, the pure model, the module and the pysim testbench."))
    dag.add(SourceStep(artifact="scenarios_source", path=_SOURCE_DIR / "scenarios.py",
                       description="The scenarios, their expected responses, and the checker."))

    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifacts=("model_done",)))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifacts=("pysim_done",),
                      which="pysim"))
    for s in TIMING:
        dag.add(ExtractPyTimingStep(name=f"extract_py_timing_{s}", scenario=s))

    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=MaxpoolAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifacts=("csim_done",)))
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(
        name="inspect_synth", comp_class=MaxpoolAccel, top_name=TOP,
        elaborate_params=("in_bw", "out_bw"), params={"in_bw": W, "out_bw": W},
        report_artifact="report_dir", seconds_artifact=None,
        output=Path("results/resources.json"), output_artifact="resources",
        require_ii1=True, loop_csv=Path("results/loop_df.csv"), loop_artifact="loop_df"))
    dag.add(CheckStep(name="check_cosim", stage="cosim", done_artifacts=("cosim_done",)))
    dag.add(CosimTxStep(name="cosim_tx"))
    for s in TIMING:
        dag.add(CosimTimingSplitStep(name=f"cosim_timing_{s}", scenario=s))
        dag.add(ValidateTimingStep(
            name=f"validate_timing_{s}", py_timing_artifact=f"py_timing_{s}",
            cosim_timing_artifact=f"cosim_timing_{s}", tolerance_cycles=20,
            output_path=f"results/timing_verdict_{s}.json",
            verdict_artifact=f"timing_verdict_{s}"))
    dag.add(ThroughputStep(name="throughput"))
    dag.add(SummaryStep(name="summary"))
    return dag


def main() -> None:
    run_dag_cli(
        build_maxpool_dag,
        description="Build the streaming 2-D max-pool accelerator.",
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
