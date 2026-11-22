"""子进程环境、目录链接（Linux 上的等价做法；Windows 上的分支要在 win-gpu 上验）。"""
import os

from ttsapi import osutil


def test_child_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/env/python")
    monkeypatch.setenv("MPLBACKEND", "module://matplotlib_inline.backend_inline")
    env = osutil.child_env(["/a", "/b"], ["/bin1", None], FOO=1)
    assert env["PYTHONPATH"] == os.pathsep.join(["/a", "/b"])
    assert env["PATH"].startswith("/bin1" + os.pathsep) and env["PATH"].endswith(os.environ["PATH"])
    assert env["MPLBACKEND"] == "Agg" and env["FOO"] == "1"


def test_link_dir(tmp_path):
    (tmp_path / "t").mkdir()
    osutil.link_dir(tmp_path / "l", tmp_path / "t")
    osutil.link_dir(tmp_path / "l", tmp_path / "t")  # 已存在不动
    (tmp_path / "t" / "f").write_text("x")
    assert (tmp_path / "l" / "f").read_text() == "x"


def test_detached_kwargs():
    assert osutil.detached_popen_kwargs() == ({"start_new_session": True} if not osutil.IS_WINDOWS else
                                              {"creationflags": osutil.NO_WINDOW | osutil.CREATE_BREAKAWAY_FROM_JOB})


def test_cuda_lib_dirs(tmp_path):
    lib = tmp_path / "venv" / "lib" / "python3.10" / "site-packages" / "nvidia" / "cublas" / "lib"
    lib.mkdir(parents=True)
    dirs = osutil.cuda_lib_dirs(tmp_path / "venv" / "bin" / "python")
    assert dirs == ([] if osutil.IS_WINDOWS else [str(lib)])
    env = osutil.child_env([], [], dirs)
    if dirs:
        assert env["LD_LIBRARY_PATH"].startswith(str(lib) + os.pathsep)
