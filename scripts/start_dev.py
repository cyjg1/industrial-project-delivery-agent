from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
INSTANCE_PATH = PROJECT_ROOT / ".dev-run.json"
BACKEND_HOST = "127.0.0.1"
PREFERRED_BACKEND_PORT = 8000
PREFERRED_FRONTEND_PORT = 5173
PORT_SEARCH_SPAN = 50
BACKEND_WAIT_SECONDS = 90
FRONTEND_WAIT_SECONDS = 90
GRACEFUL_WAIT_SECONDS = 5
KILL_TIMEOUT_SECONDS = 5
API_KEY_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "DASHSCOPE_API_KEY",
    "GLM_API_KEY",
)
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
STOP_COMMANDS = {"q", "quit", "exit", "stop"}


class ProcessExited(Exception):
    def __init__(self, returncode: int | None) -> None:
        super().__init__(f"process exited with {returncode}")
        self.returncode = returncode


@dataclass
class ChildProcess:
    name: str
    proc: subprocess.Popen[bytes]
    port: int


def log(message: str) -> None:
    print(f"[start] {message}", flush=True)


def fail(message: str) -> None:
    log(f"失败：{message}")
    raise SystemExit(1)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def is_port_available(host: str, port: int) -> bool:
    if port_in_use(host, port):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host: str, preferred: int, label: str, reserved: set[int]) -> int:
    last_port = preferred + PORT_SEARCH_SPAN - 1
    for port in range(preferred, last_port + 1):
        if port in reserved or not is_port_available(host, port):
            continue
        if port != preferred:
            log(f"{label}默认端口 {preferred} 不可用，改用 {port}。")
        return port
    fail(f"{label}在 {preferred}–{last_port} 范围内找不到可用端口。")


def command_exists(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS:
        return shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")
    return None


def run_hidden(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.run(command, **kwargs)


def spawn(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        executable = Path(command[0])
        if executable.suffix.lower() in {".exe", ".bat"} or command[0] == sys.executable:
            return subprocess.Popen(command, **kwargs)
        return subprocess.Popen(["cmd", "/c", *command], **kwargs)
    return subprocess.Popen(command, start_new_session=True, **kwargs)


def listener_pids(port: int) -> set[int]:
    pids: set[int] = set()
    current = {os.getpid(), os.getppid()}
    if IS_WINDOWS:
        try:
            result = run_hidden(["netstat", "-ano", "-p", "tcp"], timeout=KILL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return pids
        suffix = f":{port}"
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            if parts[3].upper() != "LISTENING":
                continue
            if not parts[1].endswith(suffix):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid not in current and pid > 0:
                pids.add(pid)
        return pids

    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = run_hidden(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                timeout=KILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return pids
        for raw_line in result.stdout.splitlines():
            try:
                pid = int(raw_line.strip())
            except ValueError:
                continue
            if pid not in current and pid > 0:
                pids.add(pid)
    return pids


def kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if IS_WINDOWS:
        try:
            run_hidden(["taskkill", "/F", "/PID", str(pid)], timeout=KILL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass


def request_graceful_stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, AttributeError):
        try:
            proc.terminate()
        except OSError:
            pass


def wait_proc(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def stop_child(child: ChildProcess, *, graceful: bool = True) -> None:
    proc = child.proc
    if graceful:
        request_graceful_stop(proc)
        if wait_proc(proc, GRACEFUL_WAIT_SECONDS):
            return
    for pid in listener_pids(child.port):
        kill_pid(pid)
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        if not wait_proc(proc, 2):
            try:
                proc.kill()
            except OSError:
                pass
            wait_proc(proc, 2)
    for pid in listener_pids(child.port):
        kill_pid(pid)


def ignore_further_interrupts() -> None:
    if IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
        except (AttributeError, OSError):
            pass
        return
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def write_instance(children: list[ChildProcess]) -> None:
    payload = {
        "backend_port": next((child.port for child in children if child.name == "后端"), None),
        "frontend_port": next((child.port for child in children if child.name == "前端"), None),
        "pids": [child.proc.pid for child in children if child.proc.pid],
        "ports": [child.port for child in children],
    }
    INSTANCE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_instance() -> dict[str, object]:
    if not INSTANCE_PATH.exists():
        return {}
    try:
        payload = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_instance() -> None:
    try:
        INSTANCE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def wait_http(url: str, timeout: float, label: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ProcessExited(proc.returncode)
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok"):
                return
            last_error = f"health 返回 {payload}"
        except urllib.error.HTTPError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.4)
    fail(f"{label}在 {timeout:.0f}s 内未就绪。{last_error}")


def wait_port(host: str, port: int, timeout: float, label: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ProcessExited(proc.returncode)
        if port_in_use(host, port):
            return
        time.sleep(0.3)
    fail(f"{label}在 {timeout:.0f}s 内未监听 {host}:{port}")


def abandon_port_attempt(children: list[ChildProcess], child: ChildProcess, returncode: int | None) -> None:
    if child in children:
        children.remove(child)
    stop_child(child, graceful=False)
    if is_port_available(BACKEND_HOST, child.port):
        fail(f"{child.name}启动失败，退出码 {returncode}。请查看上方日志。")
    log(f"{child.name}端口 {child.port} 启动时被占用，继续尝试下一个端口。")


def start_backend(
    children: list[ChildProcess],
    reserved: set[int],
    env: dict[str, str],
) -> ChildProcess:
    last_port = PREFERRED_BACKEND_PORT + PORT_SEARCH_SPAN - 1
    for _ in range(PORT_SEARCH_SPAN):
        port = choose_port(BACKEND_HOST, PREFERRED_BACKEND_PORT, "后端", reserved)
        reserved.add(port)
        log(f"启动后端 http://{BACKEND_HOST}:{port} ...")
        proc = spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                BACKEND_HOST,
                "--port",
                str(port),
            ],
            PROJECT_ROOT,
            env=env,
        )
        child = ChildProcess("后端", proc, port)
        children.append(child)
        try:
            wait_http(
                f"http://{BACKEND_HOST}:{port}/api/health",
                BACKEND_WAIT_SECONDS,
                "后端",
                proc,
            )
        except ProcessExited as exc:
            abandon_port_attempt(children, child, exc.returncode)
            continue
        log("后端已就绪。")
        return child
    fail(f"后端在 {PREFERRED_BACKEND_PORT}–{last_port} 范围内多次启动失败。")


def start_frontend(
    children: list[ChildProcess],
    reserved: set[int],
    env: dict[str, str],
) -> ChildProcess:
    last_port = PREFERRED_FRONTEND_PORT + PORT_SEARCH_SPAN - 1
    for _ in range(PORT_SEARCH_SPAN):
        port = choose_port(BACKEND_HOST, PREFERRED_FRONTEND_PORT, "前端", reserved)
        reserved.add(port)
        log(f"启动前端 http://{BACKEND_HOST}:{port} ...")
        proc = spawn(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                BACKEND_HOST,
                "--port",
                str(port),
                "--strictPort",
            ],
            FRONTEND_DIR,
            env=env,
        )
        child = ChildProcess("前端", proc, port)
        children.append(child)
        try:
            wait_port(BACKEND_HOST, port, FRONTEND_WAIT_SECONDS, "前端", proc)
        except ProcessExited as exc:
            abandon_port_attempt(children, child, exc.returncode)
            continue
        return child
    fail(f"前端在 {PREFERRED_FRONTEND_PORT}–{last_port} 范围内多次启动失败。")


def ensure_python_deps() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        requirements = PROJECT_ROOT / "requirements.txt"
        log("缺少 Python 依赖，正在安装 requirements.txt ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(requirements)])


def env_has_api_key(env_text: str) -> bool:
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in API_KEY_NAMES and value.strip().strip("'\""):
            return True
    return False


def ensure_env_file() -> None:
    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"
    if not env_path.exists():
        if not example_path.exists():
            log("警告：未找到 .env，后端可能无法调用模型。")
            return
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        log("未找到 .env，已创建无密钥本地配置。请在页面的“模型 API”按钮中输入评委自己的密钥。")
        return
    if not env_has_api_key(env_path.read_text(encoding="utf-8")):
        log("当前未配置模型 API Key；页面启动后可通过输入区的“模型 API”按钮配置。")


def ensure_demo_workspace() -> None:
    script = PROJECT_ROOT / "scripts" / "init_demo.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"合成演示数据初始化失败：{result.stderr.strip() or result.stdout.strip()}")
    log("合成演示工作区已就绪。")


def ensure_frontend_deps() -> None:
    if (FRONTEND_DIR / "node_modules").is_dir():
        return
    log("未找到 frontend/node_modules，正在 npm install ...")
    npm_install = spawn(["npm", "install"], FRONTEND_DIR)
    code = npm_install.wait()
    if code != 0:
        fail(f"npm install 失败，退出码 {code}")


def start_command_reader(stop_event: threading.Event) -> None:
    if not sys.stdin or not sys.stdin.isatty():
        return

    def _read() -> None:
        try:
            for raw_line in sys.stdin:
                if stop_event.is_set():
                    return
                if raw_line.strip().lower() in STOP_COMMANDS:
                    stop_event.set()
                    return
        except (OSError, UnicodeDecodeError, EOFError):
            return

    threading.Thread(target=_read, name="dev-stop-reader", daemon=True).start()


def install_stop_handler(stop_event: threading.Event) -> object | None:
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(ctrl_type: int) -> bool:
            if ctrl_type in {0, 1, 2}:
                stop_event.set()
                return True
            return False

        callback = handler_type(_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
        return callback

    def _handler(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return None


def shutdown_children(children: list[ChildProcess]) -> None:
    ignore_further_interrupts()
    if not children:
        clear_instance()
        return
    log("正在停止前后端...")
    for child in reversed(list(children)):
        log(f"停止{child.name}（端口 {child.port}）...")
        stop_child(child, graceful=True)
        if child in children:
            children.remove(child)
    clear_instance()
    log("已停止。")


def stop_running_instance() -> int:
    payload = read_instance()
    ports = {PREFERRED_BACKEND_PORT, PREFERRED_FRONTEND_PORT}
    for key in ("backend_port", "frontend_port"):
        value = payload.get(key)
        if isinstance(value, int):
            ports.add(value)
    extra_ports = payload.get("ports")
    if isinstance(extra_ports, list):
        ports.update(port for port in extra_ports if isinstance(port, int))
    pids = payload.get("pids")
    log("正在停止已启动的本地服务...")
    ignore_further_interrupts()
    if isinstance(pids, list):
        for pid in pids:
            if isinstance(pid, int):
                kill_pid(pid)
    killed = False
    for port in sorted(ports):
        listeners = listener_pids(port)
        if not listeners and not port_in_use(BACKEND_HOST, port):
            continue
        log(f"释放端口 {port} ...")
        for pid in listeners:
            kill_pid(pid)
            killed = True
        deadline = time.monotonic() + GRACEFUL_WAIT_SECONDS
        while time.monotonic() < deadline and port_in_use(BACKEND_HOST, port):
            for pid in listener_pids(port):
                kill_pid(pid)
            time.sleep(0.2)
    clear_instance()
    if killed or payload:
        log("已停止。")
    else:
        log("没有发现正在运行的本地服务。")
    return 0


def run_dev() -> int:
    os.chdir(PROJECT_ROOT)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    log(f"项目根目录：{PROJECT_ROOT}")

    if command_exists("npm") is None:
        fail("未找到 npm。请先安装 Node.js：https://nodejs.org/")

    ensure_python_deps()
    ensure_env_file()
    ensure_demo_workspace()
    ensure_frontend_deps()

    children: list[ChildProcess] = []
    reserved_ports: set[int] = set()
    stop_event = threading.Event()
    handler_ref = install_stop_handler(stop_event)
    start_command_reader(stop_event)

    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"

    try:
        backend = start_backend(children, reserved_ports, child_env)
        frontend_env = child_env.copy()
        frontend_env["PDA_API_PROXY_TARGET"] = f"http://{BACKEND_HOST}:{backend.port}"
        frontend = start_frontend(children, reserved_ports, frontend_env)
        write_instance(children)
        frontend_url = f"http://{BACKEND_HOST}:{frontend.port}"
        log(f"前端已就绪。浏览器打开 {frontend_url}")
        log(f"后端 API：http://{BACKEND_HOST}:{backend.port}")
        try:
            webbrowser.open(frontend_url)
        except Exception:
            pass

        log("停止服务：在此窗口输入 q 后回车。")
        log("也可以另开一个终端运行 stop.bat / ./stop.sh。")
        while not stop_event.wait(0.5):
            for child in children:
                code = child.proc.poll()
                if code is not None:
                    log(f"{child.name}已退出，退出码 {code}。正在收尾。")
                    return 1 if code else 0
        return 0
    except KeyboardInterrupt:
        stop_event.set()
        return 0
    finally:
        shutdown_children(children)
        del handler_ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键启动或停止本地前后端。")
    parser.add_argument("--stop", action="store_true", help="停止已启动的本地服务")
    args = parser.parse_args(argv)
    if args.stop:
        return stop_running_instance()
    return run_dev()


if __name__ == "__main__":
    raise SystemExit(main())
