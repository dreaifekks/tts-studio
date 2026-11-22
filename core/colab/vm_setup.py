# 在 Colab VM 上跑（ttsapi/compute/colab/job.py 放到后台）：代码 bundle 和补丁 → tts-core 和 GPT-SoVITS，输入资料库 → /content/library
# （数据集 tar 留在资料库里，VM 的 gateway 第一次用到时按正式逻辑解压到 GPT-SoVITS 的 datasets/.unpacked/，见
# ttsapi.datasets.local_root），scripts/install_linux.sh 装环境、从 HF 下底模。
# 每一步记进 /content/job/timeline.jsonl。配置见 /content/job/job.json。STEPS 环境变量可以只跑其中几步（本机模拟用）。
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, "/content/job")
from vm_lib import (API, CFG, GSV, GSV_UPSTREAM, GSV_V5, GSV_V5_COMMIT, INPUTS, JOB, LIB,  # noqa: E402
                    dir_bytes, mark, sh, wants_v5)

STEPS = os.environ.get("STEPS", "code,library,env").split(",")
mark("setup", "start", gpu=subprocess.run("nvidia-smi --query-gpu=name --format=csv,noheader", shell=True,
                                           capture_output=True, text=True).stdout.strip())


def setup_gsv() -> str:
    patch = INPUTS / CFG["gsv_patch"]
    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    if patch_sha != CFG["gsv_patch_sha256"]:
        raise ValueError(f"GPT-SoVITS patch SHA-256 mismatch: {patch.name}")

    base, branch = CFG["gsv_base"], CFG["gsv_branch"]
    marker = GSV / ".tts-studio-patch.sha256"
    message = f"Apply TTS Studio patch {patch_sha}"

    def git(*args, check=True, env=None):
        result = subprocess.run(["git", "-C", str(GSV), *args], capture_output=True, text=True, env=env)
        if check and result.returncode:
            print(result.stderr, file=sys.stderr, end="", flush=True)
            result.check_returncode()
        return result

    if not (GSV / ".git").exists():
        GSV.mkdir(parents=True, exist_ok=True)
        git("init", "-q")
        git("remote", "add", "origin", GSV_UPSTREAM)
    if git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode:
        git("fetch", "-q", "--depth", "1", GSV_UPSTREAM, base)

    # The branch must still point to the expected patch commit, including after an interrupted setup.
    target = f"refs/heads/{branch}"
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == patch_sha:
        cached = git("show", "-s", "--format=%P%n%s", target, check=False)
        if cached.returncode == 0 and cached.stdout.splitlines() == [base, message]:
            git("checkout", "-q", "-f", "-B", branch, target)
            return git("rev-parse", "--short=7", "HEAD").stdout.strip()

    git("checkout", "-q", "-f", "-B", branch, base)
    git("apply", "--index", "--whitespace=nowarn", str(patch))
    identity = dict(os.environ, GIT_AUTHOR_NAME="TTS Studio", GIT_AUTHOR_EMAIL="noreply@tts-studio.local",
                    GIT_COMMITTER_NAME="TTS Studio", GIT_COMMITTER_EMAIL="noreply@tts-studio.local")
    git("-c", "commit.gpgSign=false", "commit", "-q", "-m", message, env=identity)
    marker.write_text(f"{patch_sha}\n", encoding="ascii")
    return git("rev-parse", "--short=7", "HEAD").stdout.strip()


def step_code():
    """从控制端 bundle 取 tts-core，GPT-SoVITS 使用公开上游和作业附带的补丁。"""
    mark("code", "start")
    api = INPUTS / CFG["ttsapi_bundle"]
    gsv_commit = setup_gsv()
    v5_mark = {}
    if wants_v5():
        v5_commit = CFG.get("gsv_v5_commit", GSV_V5_COMMIT)
        if (GSV_V5 / ".git").exists():
            sh(f"cd {GSV_V5} && (git cat-file -e {v5_commit}^{{commit}} 2>/dev/null || "
               f"git fetch -q --depth 1 origin {v5_commit}) && git checkout -q -f {v5_commit} && git log --oneline -1")
        else:
            sh(f"git init -q {GSV_V5} && cd {GSV_V5} && "
               f"git remote add origin https://github.com/RVC-Boss/GPT-SoVITS.git && "
               f"git fetch -q --depth 1 origin {v5_commit} && git checkout -q -f {v5_commit} && git log --oneline -1")
        v5_mark["gsv_v5"] = v5_commit[:7]
    sh(f"git clone -q {api} {API} && cd {API} && git checkout -q {CFG['ttsapi_commit']} && git log --oneline -1")
    mark("code", ttsapi=CFG["ttsapi_commit"][:7], gsv=gsv_commit, **v5_mark)


def step_library():
    """输入资料库移到 /content/library。数据集 tar 留着：gateway 第一次用到时核对 sha256、解压到数据根的缓存（和控制端同一套
    逻辑）；结果打包时不带 tar（vm_pack.py）。"""
    mark("library", "start")
    (INPUTS / "library").rename(LIB)
    mark("library", bytes=dir_bytes(LIB))


def step_env():
    mark("env", "start")
    env = dict(os.environ, GSV_ROOT=str(GSV), PRETRAINED="hf", ENGINES=",".join(CFG["engines"]))
    if wants_v5():
        env.update(GSV_V5_ROOT=str(GSV_V5), GSV_V5_COMMIT=CFG.get("gsv_v5_commit", GSV_V5_COMMIT))
    sh(f"bash {API}/scripts/install_linux.sh", env=env)
    sh(f"uv pip freeze -p {GSV}/.venv/bin/python > {JOB}/venv-freeze-colab.txt")
    mark("env")


for name in STEPS:
    globals()[f"step_{name}"]()
mark("setup")
