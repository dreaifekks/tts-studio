"""Run an expanded job on a VM: upload the inputs, run the repository's VM scripts, download the result tar."""

import json
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .run import ColabRun, RunState

VM_SCRIPTS = Path(__file__).resolve().parents[3] / "colab"
SCRIPTS = ("vm_lib.py", "vm_setup.py", "vm_run.py", "vm_pack.py")
# 接手上一个任务留下的 VM（ColabRun.reused）时先清掉上次的作业目录、资料库、tts-core、输入和结果；
# GPT-SoVITS 连同 .venv 和底模留着，vm_setup 只核对（装环境的脚本对已装好的部分很快）
RESET = """
import glob, os, shutil
for p in ['/content/job', '/content/library', '/content/tts-api', *glob.glob('/content/tts-inputs-*'),
          *glob.glob('/content/results-*.tar.tts-*')]:
    shutil.rmtree(p, ignore_errors=True)
for p in glob.glob('/content/results-*.tar'):
    os.remove(p)
"""


def reset_reused(run):
    if getattr(run, "reused", False):
        run.execute(RESET, timeout=300)


def run_job(run, spec, inputs, output, stage=lambda step: None, cancel=None):
    """Run an expanded job.json. inputs maps paths below the VM input directory to local files.

    stage(step) is called from this thread before "upload", "setup", "run" and "download".
    The result tar (layout in colab/vm_pack.py) is downloaded unless the VM is gone.
    If cancel (a threading.Event) is set, stops before entering any subsequent phase.
    """
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise RuntimeError("job cancelled")

    def wait_run():
        if cancel is not None and isinstance(run, ColabRun):
            return run.wait(cancel=cancel)
        return run.wait()

    job_id = uuid.uuid4().hex
    mirror = f"/content/tts-inputs-{job_id}"
    config = dict(spec, inputs=mirror)
    run_id = config["run_id"]
    if not run_id or PurePosixPath(run_id).name != run_id or run_id == "..":
        raise ValueError("run_id must be one path component")
    check_cancel()
    stage("upload")
    reset_reused(run)
    for rel, local in inputs.items():
        check_cancel()
        dest = PurePosixPath(rel)
        if dest.is_absolute() or ".." in dest.parts:
            raise ValueError("input path must be relative to the input directory")
        run.upload(local, f"{mirror}/{dest}")
    for name in SCRIPTS:
        check_cancel()
        run.upload(VM_SCRIPTS / name, f"/content/job/{name}")
    check_cancel()
    with tempfile.TemporaryDirectory() as folder:
        spec_file = Path(folder) / "job.json"
        spec_file.write_text(json.dumps(config))
        run.upload(spec_file, "/content/job/job.json")
    for script in ("vm_setup", "vm_run"):
        check_cancel()
        stage(script.removeprefix("vm_"))
        run.run_background(
            f"cd /content/job && python3 {script}.py",
            f"/content/job/{job_id}-{script}.exit",
            heartbeat="/content/job/job.json",
        )
        state = wait_run()
        check_cancel()
        if state != RunState.DONE:
            break
    check_cancel()
    if state != RunState.GONE:
        # Pack whatever exists, so logs come back even when setup or a task failed.
        archive = f"/content/results-{job_id}.tar"
        run.run_background(
            f"cd /content/job && python3 vm_pack.py {archive}",
            f"/content/{job_id}-pack.exit",
        )
        if wait_run() != RunState.DONE:
            raise RuntimeError("Colab result archive failed")
        check_cancel()
        stage("download")
        run.download(archive, output, timeout=1800)
    return state
