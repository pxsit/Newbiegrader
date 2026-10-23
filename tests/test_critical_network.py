"""Socket creation only: no Internet/host connections or real user workloads."""
from contextlib import ExitStack
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import grader
import job_sandbox
from critical_helpers import test_server


class CriticalNetworkPolicy(unittest.TestCase):
    def evaluate(self, machine, arch, number, domain=0, kind=0):
        instructions = job_sandbox._network_filter_program(machine)
        index, accumulator = 0, 0
        while index < len(instructions):
            code, yes, no, value = instructions[index]
            if code == 0x20:  # BPF_LD W ABS
                accumulator = {0: number, 4: arch, 16: domain, 24: kind}[value]
            elif code == 0x54:  # AND K
                accumulator &= value
            elif code in (0x15, 0x35):  # JEQ / JGE
                condition = accumulator == value if code == 0x15 else accumulator >= value
                index += yes if condition else no
            elif code == 0x06:  # RET K
                return value
            else:
                self.fail("unexpected filter instruction")
            index += 1
        self.fail("filter did not return")

    def test_critical_native_network_and_io_uring_calls_are_denied(self):
        for machine, arch, socket, socketpair in (("x86_64", 0xc000003e, 41, 53),
                                                ("aarch64", 0xc00000b7, 198, 199)):
            for number in (socket, socketpair, 425, 426, 427):
                with self.subTest(machine=machine, syscall=number):
                    self.assertEqual(self.evaluate(machine, arch, number), 0x50000 | errno.EPERM)
            ordinary = ((0, 1, 9, 56, 57, 59, 60, 63, 202, 231)
                        if machine == 'x86_64' else (63, 64, 93, 94, 98, 220, 221, 222))
            for number in ordinary:
                self.assertEqual(self.evaluate(machine, arch, number), 0x7fff0000)
            for flags in (0, 0x80000, 0x800, 0x80800):
                self.assertEqual(self.evaluate(machine, arch, socketpair, 1, 1 | flags), 0x7fff0000)
            for domain, kind in ((2, 1), (10, 1), (1, 2), (1, 3), (1, 5)):
                self.assertEqual(self.evaluate(machine, arch, socketpair, domain, kind), 0x50001)

    def test_persistent_ipc_keyrings_bpf_and_perf_are_denied_on_both_abis(self):
        calls = {
            'x86_64': (0xc000003e, (29, 30, 31, 64, 65, 66, 67, 68, 69, 70, 71,
                                   220, 248, 249, 250, 298, 321)),
            'aarch64': (0xc00000b7, (*range(186, 198), 217, 218, 219, 241, 280)),
        }
        for machine, (arch, numbers) in calls.items():
            for number in numbers:
                with self.subTest(machine=machine, syscall=number):
                    self.assertEqual(self.evaluate(machine, arch, number), 0x50001)

    def test_critical_foreign_abi_and_x32_cannot_bypass_filter(self):
        for machine, arch in (("x86_64", 0xc000003e), ("aarch64", 0xc00000b7)):
            for foreign in (0x40000003, 0x40000028, 0):
                self.assertEqual(self.evaluate(machine, foreign, 41), 0x80000000)
            self.assertEqual(self.evaluate(machine, arch, 0x40000000 | 41), 0x80000000)
        with self.assertRaises(OSError):
            job_sandbox._network_filter_program("unsupported-synthetic-architecture")


SOCKET_PROBE = (
    'import errno,socket\n'
    'for family in (socket.AF_INET,socket.AF_INET6,socket.AF_UNIX,socket.AF_NETLINK):\n'
    ' for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):\n'
    '  try:\n'
    '   sock=socket.socket(family,kind);sock.close();raise AssertionError("socket allowed")\n'
    '  except OSError as e:assert e.errno==errno.EPERM,e\n'
    'a,b=socket.socketpair();a.send(b"synthetic");assert b.recv(9)==b"synthetic";a.close();b.close()\n'
    'try:\n'
    ' a,b=socket.socketpair(type=socket.SOCK_DGRAM);a.close();b.close();raise AssertionError("datagram pair allowed")\n'
    'except OSError as e:assert e.errno==errno.EPERM,e\n'
)

# Invalid IDs/keys/pointers: even if the filter regresses, this probe does not
# create IPC resources, load BPF programs or install keys in the host kernel.
SENSITIVE_PROBE = (
    'import ctypes,errno,platform\n'
    'calls={"x86_64":(29,30,31,64,65,66,67,68,69,70,71,220,248,249,250,298,321),'
    '"aarch64":(*range(186,198),217,218,219,241,280)}[platform.machine()]\n'
    'libc=ctypes.CDLL(None,use_errno=True)\n'
    'for nr in calls:\n'
    ' ctypes.set_errno(0)\n'
    ' rc=libc.syscall(ctypes.c_long(nr),ctypes.c_long(-1),0,0,0,0,0)\n'
    ' assert rc==-1 and ctypes.get_errno()==errno.EPERM,(nr,rc,ctypes.get_errno())\n'
)


class CriticalNetworkExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            reason = "[CRITICAL-SKIP] actual seccomp checks require Linux/root"
            print(reason, file=sys.stderr, flush=True)
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.base.chmod(0o755)
        self.stack.enter_context(patch.dict(os.environ, {
            "GRADER_SANDBOX_IDENTITY_DIR": str(self.base / "identities"),
            "GRADER_REQUIRE_NETNS": "0"}))

    def test_critical_runtime_and_exec_descendant_cannot_create_sockets(self):
        server = test_server()
        source = SOCKET_PROBE + (
            'import subprocess,sys,asyncio\n'
            f'subprocess.run([sys.executable,"-c",{SOCKET_PROBE!r}],check=True)\n'
            'async def benign():await asyncio.sleep(.001)\n'
            'asyncio.run(benign())\n'
            'print("synthetic-network-denied")\n')
        result = server._run_playground(None, ".py", source, "", 5, 128)
        self.assertEqual(result.get("verdict"), "OK", result)
        self.assertEqual(result["stdout"], "synthetic-network-denied\n")

    def test_critical_job_cannot_reach_postgres_or_its_credentials(self):
        # Real listeners stand in for PostgreSQL's Unix socket and TCP port. The
        # parent holds database settings, so any leak into the job shows up.
        server = test_server()
        unix_path = str(self.base / ".s.PGSQL.5432")
        with socket.socket(socket.AF_UNIX) as unix, socket.socket(socket.AF_INET) as tcp:
            unix.bind(unix_path)
            unix.listen()
            tcp.bind(("127.0.0.1", 0))
            tcp.listen()
            self.stack.enter_context(patch.dict(os.environ, {
                "GRADER_DB_DSN": f"host={self.base} dbname=newbiegrader",
                "PGPASSWORD": "synthetic-secret", "PGHOST": str(self.base)}))
            source = "\n".join((
                'import errno,os,socket',
                'leaked=[k for k in os.environ if k.startswith(("GRADER_DB","PG","DATABASE"))]',
                'assert not leaked,leaked',
                f'for family,target in ((socket.AF_UNIX,{unix_path!r}),(socket.AF_INET,("127.0.0.1",{tcp.getsockname()[1]}))):',
                ' try:',
                '  s=socket.socket(family,socket.SOCK_STREAM);s.connect(target);raise AssertionError("database reachable")',
                ' except OSError as e:assert e.errno==errno.EPERM,e',
                'print("database-unreachable")',
                ''))
            result = server._run_playground(None, ".py", source, "", 5, 128)
        self.assertEqual(result.get("verdict"), "OK", result)
        self.assertEqual(result["stdout"], "database-unreachable\n")

    def test_critical_compile_launcher_has_the_same_network_filter(self):
        with job_sandbox.scope():
            workspace = self.base / "compile"
            workspace.mkdir()
            grader.prepare_workspace(workspace)
            result = grader._compile([sys.executable, "-c", SOCKET_PROBE + 'print("denied")\n'], workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "denied\n")

    def test_persistent_resources_are_denied_in_runtime_and_exec_descendant(self):
        server = test_server()
        source = SENSITIVE_PROBE + (
            'import subprocess,sys,threading,os\n'
            f'subprocess.run([sys.executable,"-c",{SENSITIVE_PROBE!r}],check=True)\n'
            'values=[]\n'
            't=threading.Thread(target=lambda:values.append(42));t.start();t.join()\n'
            'assert values==[42]\n'
            'pid=os.fork()\n'
            'if pid==0:os._exit(0)\n'
            'assert os.waitpid(pid,0)[1]==0\n'
            'print("persistent-resources-denied")\n')
        result = server._run_playground(None, '.py', source, '', 5, 128)
        self.assertEqual(result.get('verdict'), 'OK', result)
        self.assertEqual(result['stdout'], 'persistent-resources-denied\n')

    def test_persistent_resources_are_denied_in_compile_launcher(self):
        with job_sandbox.scope():
            workspace = self.base / 'compile-ipc'
            workspace.mkdir()
            grader.prepare_workspace(workspace)
            result = grader._compile([sys.executable, '-c', SENSITIVE_PROBE], workspace)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_critical_filter_installation_failure_refuses_execution(self):
        from judge_runner import run_bounded
        with job_sandbox.scope():
            workspace = self.base / "refusal"
            workspace.mkdir()
            grader.prepare_workspace(workspace)
            code = (
                'from unittest.mock import patch\nimport grader,job_sandbox\n'
                'with job_sandbox.launch_scope(),patch.object(job_sandbox,"_install_network_filter",side_effect=OSError("synthetic failure")):\n'
                ' grader.build_limits(128,5,network=False)()\n'
                ' print("SYNTHETIC_UNTRUSTED_RAN")\n')
            result = run_bounded([sys.executable, "-c", code], "", 10,
                output_limit=4096, env=grader.judge_env(str(workspace)), cwd=str(grader.ROOT))
        self.assertEqual(result.returncode, grader.PRIVILEGE_REFUSED_RC)
        self.assertIn(grader.PRIVILEGE_REFUSED_MARKER, result.stderr)
        self.assertNotIn("SYNTHETIC_UNTRUSTED_RAN", result.stdout)

    def test_critical_boot_probe_and_fail_closed_startup(self):
        import asyncio
        self.assertIsNone(job_sandbox.verify_network_filter())
        server = test_server()
        with patch.object(server, "GRADER_ROLE", "combined"), \
                patch.object(server, "QUEUE_MODE", "local"), \
                patch.object(server, "PROBLEM_SOURCE", "local"), \
                patch.dict(os.environ, {"GRADER_ALLOW_UNISOLATED_JUDGE": "1"}), \
                patch.object(job_sandbox, "verify_network_filter", side_effect=OSError("synthetic unavailable")):
            with self.assertRaisesRegex(RuntimeError, "network syscall filter"):
                asyncio.run(server.validate_runtime())
