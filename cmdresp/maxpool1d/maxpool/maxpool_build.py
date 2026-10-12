"""maxpool_build.py -- the build pipeline for the streaming 1-D max-pool accelerator.

    python maxpool_build.py --list-steps
    python maxpool_build.py --through check_pysim       # Python only, no Vitis
    python maxpool_build.py --through summary           # everything, Vitis included

The stream word is 32 bits (the frame's target).  The pipeline, in reading order:

    check_eval       maxpool_eval against the hand-computed examples and the NumPy reference
    scenarios        write each scenario's stimulus and expected response (scenarios.py)
    py_model         run the pure bit-exact model on every scenario
    check_model      ...and compare it with the expected response
    py_sim           run pysim (functional only) on the scenarios a pysim stream can express
    check_pysim      ...compare
    gen_include      schema headers, serializers and the stream/testbench helpers
    sources          the hand-written C++ in place (a build outside this directory)
    gen_kernel       the kernel boundary (gen/maxpool.hpp, gen/maxpool.cpp); the body is the
                     hand-written maxpool_body_impl.tpp
    csim             Vitis C simulation, every scenario, hand-written maxpool_tb.cpp
    check_csim       ...compare
    csynth_w32       C synthesis, then ONE RTL co-simulation of every scenario in order
    inspect_synth_w32  loops, II and resources from the synthesis report
    check_cosim      the co-simulated responses, compared
    extract_cosim_timing_w32   the cosim report's latency numbers
    summary          every check, the synthesis report and the cosim timing

Every check compares against the same expected response, computed from each scenario's
intent (scenarios.py) rather than from any implementation.  By instruction there is no
Python timing model: pysim is functional, and every cycle count comes from the RTL.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.cosim_steps import ExtractCosimTimingStep
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.resource_steps import InspectSynthStep
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import read_bursts, write_bursts

try:
    from examples.stream_inband import scenarios as S
    from examples.stream_inband.maxpool import (
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, Int16, MaxpoolAccel, MaxpoolTB, connect,
        maxpool_stream_model,
    )
except ModuleNotFoundError:  # run from inside the project directory
    import scenarios as S  # type: ignore[no-redef]
    from maxpool import (  # type: ignore[no-redef]
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, Int16, MaxpoolAccel, MaxpoolTB, connect,
        maxpool_stream_model,
    )

_SOURCE_DIR = Path(__file__).resolve().parent

#: The stream widths every step covers.
WIDTHS = tuple(WORD_BW_SUPPORTED)

#: The kernel top for each width.
TOPS = {32: "maxpool"}

#: The hand-written sources Vitis needs beside the generated ones.  A build in another
#: directory gets a copy.
HAND_WRITTEN = ("run.tcl", "maxpool_tb.cpp", "maxpool_body_impl.tpp")


def width_dir(root: Path, word_bw: int) -> Path:
    """One width's scenario data: ``data/w32``."""
    return root / "data" / f"w{word_bw}"


def _ensure_sources(root: Path) -> None:
    for name in HAND_WRITTEN:
        if not (root / name).exists():
            shutil.copy(_SOURCE_DIR / name, root / name)


_STUB_MARK = "TODO: implement body"


def _require_real_body(root: Path) -> None:
    """Refuse to simulate the generated stub (see the stream_inband reference)."""
    body = root / "maxpool_body_impl.tpp"
    if _STUB_MARK in body.read_text(encoding="utf-8"):
        raise RuntimeError(f"{body} is the generated stub, not the kernel body: copy the "
                           f"hand-written maxpool_body_impl.tpp into the build directory")


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


def _names(root: Path, word_bw: int) -> list[str]:
    return (width_dir(root, word_bw) / "scenarios.txt").read_text(encoding="utf-8").split()


def _meta(root: Path, word_bw: int, name: str) -> dict:
    return json.loads((width_dir(root, word_bw) / name / "scenario.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Python: the function, scenarios, the pure model, pysim
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class CheckEvalStep(BuildStep):
    """Run tests/test_maxpool_eval.py: the worked examples and the NumPy comparison."""
    description = "Check maxpool_eval against hand-computed examples and the NumPy reference."
    consumes = ["maxpool_source"]
    produces = {"check_eval": Path("results/check_eval.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        r = subprocess.run([sys.executable, str(_SOURCE_DIR / "tests" / "test_maxpool_eval.py")],
                           capture_output=True, text=True)
        out = config.root_dir / "results" / "check_eval.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(r.stdout + r.stderr, encoding="utf-8")
        print(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError("maxpool_eval disagrees with a worked example or with NumPy")
        return {"check_eval": out}


@dataclass(kw_only=True)
class ScenariosStep(BuildStep):
    description = "Write every scenario's stimulus and expected response."
    consumes = ["maxpool_source", "scenarios_source"]
    produces = {"scenario_list": Path("data/scenarios.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        for bw in WIDTHS:
            S.write_scenarios(width_dir(root, bw), bw)
        out = root / "data" / "scenarios.txt"
        out.write_text("\n".join(S.scenarios()) + "\n", encoding="utf-8")
        return {"scenario_list": out}


@dataclass(kw_only=True)
class ModelStep(BuildStep):
    description = "Run the pure bit-exact model (maxpool_stream_model) on every scenario."
    consumes = ["scenario_list"]
    produces = {"model_done": Path("results/model_done.txt")}
    params = {}

    def run(self, config: BuildConfig, **_) -> dict:
        root = config.root_dir
        for bw in WIDTHS:
            for name in _names(root, bw):
                d = width_dir(root, bw) / name
                res = maxpool_stream_model(read_bursts(d / "in"), word_bw=bw)
                write_bursts(res.out, d / "model")
                _write_status(d / "model" / "status.json", res.status())
        done = root / "results" / "model_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done\n", encoding="utf-8")
        return {"model_done": done}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """pysim: the module's Python body, functional only, on every scenario it can express."""
    description = "Run pysim on every scenario a pysim stream can express."
    consumes = ["scenario_list", "maxpool_source"]
    produces = {"pysim_done": Path("results/pysim_done.txt")}
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, clk_freq, **_) -> dict:
        root = config.root_dir
        for bw in WIDTHS:
            for name in _names(root, bw):
                if not _meta(root, bw, name)["pysim"]:
                    continue
                d = width_dir(root, bw) / name
                sim = Simulation()
                clk = Clock(freq=clk_freq)
                accel = MaxpoolAccel(name="maxpool_accel", sim=sim, clk=clk, in_bw=bw, out_bw=bw)
                tb = MaxpoolTB(name="maxpool_tb", sim=sim, stimulus=d / "in", word_bw=bw,
                               n_out=len(read_bursts(d / "expected")))
                connect(sim, tb, accel, clk)
                sim.run_sim()
                write_bursts(tb.out, d / "pysim")
                _write_status(d / "pysim" / "status.json", tb.status)
        done = root / "results" / "pysim_done.txt"
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text("done\n", encoding="utf-8")
        return {"pysim_done": done}


@dataclass(kw_only=True)
class CheckStep(BuildStep):
    """Compare one stage's recorded responses with the expected ones (scenarios.check).

    ``which`` names a scenario.json flag that selects the scenarios (None: all).
    """
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
        report: dict[str, list[str]] = {}
        for bw in WIDTHS:
            names = [n for n in _names(root, bw)
                     if self.which is None or _meta(root, bw, n)[self.which]]
            for name, problems in S.check(width_dir(root, bw), self.stage, names).items():
                report[f"w{bw}/{name}"] = problems
        out = root / "results" / f"check_{self.stage}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        failed = {k: v for k, v in report.items() if v}
        for name, problems in report.items():
            print(f"    {self.stage:6s} {name:44s} {'PASS' if not problems else 'FAIL'}")
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


def _run_vitis(config: BuildConfig, stage: str, word_bw: int, live_output: bool,
               clk_freq: float) -> None:
    _ensure_sources(config.root_dir)
    # The stage goes in the environment: vitis-run 2025.1 has no --tclargs.
    env = {"WAVEFLOW_POLY_STAGE": stage, "WAVEFLOW_POLY_WIDTH": str(word_bw),
           "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
    try:
        result = toolchain.run_vitis_hls(config.root_dir / "run.tcl", work_dir=config.root_dir,
                                         capture_output=not live_output, env=env)
    except Exception as exc:  # CalledProcessError carries the Vitis log
        out = getattr(exc, "stdout", "") or ""
        raise RuntimeError(f"Vitis {stage} (w{word_bw}) failed: {exc}\n{out[-3000:]}") from exc
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
        for bw in WIDTHS:
            for name in _names(root, bw):
                (width_dir(root, bw) / name / "csim").mkdir(parents=True, exist_ok=True)
            _run_vitis(config, "csim", bw, live_output, clk_freq)
        done = root / "results" / "csim_done.txt"
        done.write_text("done\n", encoding="utf-8")
        return {"csim_done": done}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    """C synthesis, then one RTL co-simulation: the testbench walks every scenario in order,
    one kernel call per activation on fresh streams."""
    word_bw: int
    description = "Vitis C synthesis, then one RTL co-simulation of every scenario."
    params = {"live_output": False, "clk_freq": 100e6}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["maxpool_cpp", "maxpool_hpp", "maxpool_body_impl", "include_dir", "check_csim"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        w = self.word_bw
        return {f"report_dir_w{w}": Path(f"w{w}_proj/solution1"),
                f"cosim_done_w{w}": Path(f"results/cosim_done_w{w}.txt")}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        root, w = config.root_dir, self.word_bw
        for name in _names(root, w):
            (width_dir(root, w) / name / "cosim").mkdir(parents=True, exist_ok=True)
        _run_vitis(config, "synth", w, live_output, clk_freq)
        done = root / "results" / f"cosim_done_w{w}.txt"
        done.write_text("all scenarios\n", encoding="utf-8")
        return {f"report_dir_w{w}": root / f"w{w}_proj" / "solution1",
                f"cosim_done_w{w}": done}


@dataclass(kw_only=True)
class SummaryStep(BuildStep):
    """Everything in one place, and the target that runs every check."""
    description = "Collect every check, the synthesis report and the cosim timing."
    consumes = ["check_eval", "check_model", "check_pysim", "check_csim", "check_cosim",
                *[f"{a}_w{w}" for w in WIDTHS for a in ("loop_df", "resources", "cosim_timing")]]
    produces = {"summary": Path("results/summary.json")}
    params = {}

    def run(self, config: BuildConfig, **art) -> dict:
        def load(key):
            return json.loads(Path(art[key]).read_text(encoding="utf-8"))
        summary = {
            "checks": {k.removeprefix("check_"): {n: (not v) for n, v in load(k).items()}
                       for k in ("check_model", "check_pysim", "check_csim", "check_cosim")},
            "cosim_timing": {f"w{w}": load(f"cosim_timing_w{w}") for w in WIDTHS},
            "loops": {f"w{w}": Path(art[f"loop_df_w{w}"]).read_text(encoding="utf-8")
                      for w in WIDTHS},
            "resources": {f"w{w}": load(f"resources_w{w}") for w in WIDTHS},
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

    # Python: the function, the model and pysim, checked.
    dag.add(CheckEvalStep(name="check_eval"))
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifacts=("model_done",)))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifacts=("pysim_done",),
                      which="pysim"))

    # Code generation: headers, and the kernel boundary around the hand-written body.
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(SourcesStep(name="sources"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=MaxpoolAccel,
                           source_artifact="kernel_sources", output_dir="gen", impl_dir="."))

    # Vitis: csim on every scenario, then synthesis and one cosim of every scenario.
    dag.add(CSimStep(name="csim"))
    dag.add(CheckStep(name="check_csim", stage="csim", done_artifacts=("csim_done",)))
    for w in WIDTHS:
        dag.add(CSynthStep(name=f"csynth_w{w}", word_bw=w))
        dag.add(InspectSynthStep(
            name=f"inspect_synth_w{w}", comp_class=MaxpoolAccel, top_name=TOPS[w],
            elaborate_params=("in_bw", "out_bw"), params={"in_bw": w, "out_bw": w},
            report_artifact=f"report_dir_w{w}", seconds_artifact=None,
            output=Path(f"results/resources_w{w}.json"), output_artifact=f"resources_w{w}",
            require_ii1=True, loop_csv=Path(f"results/loop_df_w{w}.csv"),
            loop_artifact=f"loop_df_w{w}"))
        dag.add(ExtractCosimTimingStep(
            name=f"extract_cosim_timing_w{w}", top=TOPS[w],
            report_dir_artifact=f"report_dir_w{w}", cosim_timing_artifact=f"cosim_timing_w{w}",
            output_path=f"results/cosim_timing_w{w}.json"))
    dag.add(CheckStep(name="check_cosim", stage="cosim",
                      done_artifacts=tuple(f"cosim_done_w{w}" for w in WIDTHS)))
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
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {"clk_freq": a.clk_freq, "live_output": a.live_output},
    )


if __name__ == "__main__":
    main()
