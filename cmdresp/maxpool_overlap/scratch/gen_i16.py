import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from maxpool import Int16  # noqa: E402
from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import StreamUtilsStep  # noqa: E402
from waveflow.hw.arrayutils import ArrayUtilsStep  # noqa: E402

dag = BuildDag()
dag.add(StreamUtilsStep(output_dir="include"))
st = dag.add(ArrayUtilsStep(Int16, [32]))
print(st.name, st.build_outputs)
res = dag.run(BuildConfig(root_dir=ROOT))
for k, r in res.items():
    print(k, r.success, getattr(r, "artifacts", None), getattr(r, "message", None))
