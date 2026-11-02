"""Entry point of the standalone SAOUB Launcher (SAOUB_Launcher.exe, built by tools/launcher/build_bundle.py).

The exe only holds Python and the libraries; the launcher and the server run from the .py files next to it, so a
code update (tools/launcher/updater.py) replaces them without a new exe.
  SAOUB_Launcher.exe            the launcher window (tools/launcher/saoub_launcher.pyw)
  SAOUB_Launcher.exe --server   the game server (server/main.py), started by the launcher
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if not getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.dirname(ROOT))


def _std_streams():
    """A windowed exe has no stdout/stderr; the launcher pipes the server's output, so bind them to those pipes."""
    if sys.stdout is not None:
        return
    import ctypes
    import msvcrt
    k32 = ctypes.windll.kernel32
    for name, std in (("stdout", -11), ("stderr", -12)):
        handle = k32.GetStdHandle(std)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            setattr(sys, name, open(fd, "w", encoding="utf-8", errors="replace", buffering=1))
        except OSError:
            setattr(sys, name, open(os.devnull, "w"))


def main():
    if "--server" in sys.argv:
        sys.argv = [os.path.join(ROOT, "server", "main.py")]
        _std_streams()
        os.chdir(os.path.join(ROOT, "server"))
        sys.path.insert(0, os.path.join(ROOT, "server"))
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        script = os.path.join(ROOT, "tools", "launcher", "saoub_launcher.pyw")
        sys.argv = [script]
        runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
