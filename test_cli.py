"""
tests/test_cli.py - every sub-command, every option, and the folder-nesting traps.

    python -m unittest tests.test_cli -v

Guards against: AttributeError from a missing argparse attribute (the dispatch is exercised for ALL commands, not just
`run`), option-order surprises (`--text run`, `-v tools`), bad values, and `ModuleNotFoundError` from a project that was
extracted one folder too deep.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as alpha_main  # noqa: E402  (bootstrap() is a no-op when run from the right place)

PROJECT_ITEMS = ["main.py", "config", "core", "ui", "tools", "voice", "voice_security", "scripts"]


def run_main(*args, cwd=ROOT, stdin="", env_extra=None, main_path=None, timeout=60):
    env = dict(os.environ, ALPHA_DATA_DIR=tempfile.mkdtemp(prefix="alpha_cli_"), ALPHA_VOICE_LOCK="off", PYTHONIOENCODING="utf-8")
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "SERVER_TOKEN"):
        env.pop(k, None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, str(main_path or ROOT / "main.py"), *args], cwd=str(cwd), input=stdin, text=True,
                       capture_output=True, timeout=timeout, env=env)
    return p.returncode, p.stdout + p.stderr, env["ALPHA_DATA_DIR"]


def copy_project(dest: Path) -> None:
    for item in PROJECT_ITEMS:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, dest / item, ignore=shutil.ignore_patterns("__pycache__", "data"))
        else:
            shutil.copy2(src, dest / item)


class ParserTests(unittest.TestCase):
    parser = alpha_main.build_parser()

    def parse(self, *argv):
        return self.parser.parse_args(alpha_main.normalize_argv(list(argv)))

    def test_argv_normalisation(self):
        n = alpha_main.normalize_argv
        self.assertEqual(n([]), ["run"])
        self.assertEqual(n(["--text"]), ["run", "--text"])
        self.assertEqual(n(["--text", "run"]), ["run", "--text"])             # options before the command are kept
        self.assertEqual(n(["-v", "tools"]), ["tools", "-v"])
        self.assertEqual(n(["voice-test", "--seconds", "5"]), ["voice-test", "--seconds", "5"])
        self.assertEqual(n(["--help"]), ["--help"])
        self.assertEqual(n(["bogus"]), ["bogus"])

    def test_options_before_command_are_not_lost(self):
        a = self.parse("--text", "run")
        self.assertTrue(a.text); self.assertEqual(a.command, "run")
        a = self.parse("--no-ui", "--mute")
        self.assertTrue(a.no_ui and a.mute and a.command == "run")

    def test_every_command_declares_what_its_handler_reads(self):
        cases = [("run", [], ["text", "mute", "whisper", "no_tracking", "no_ui", "no_browser", "window", "host", "port", "verbose"]),
                 ("enroll", [], ["samples", "impostor", "verbose"]), ("enroll", ["--samples", "3", "--impostor"], ["samples", "impostor"]),
                 ("voice-test", [], ["seconds", "verbose"]), ("voice-test", ["--seconds", "5"], ["seconds"]),
                 ("voice-status", [], ["verbose"]), ("voice-reset", [], ["verbose"]), ("mic-test", [], ["verbose"]),
                 ("list-mics", [], ["verbose"]), ("tools", [], ["verbose"]), ("doctor", [], ["verbose"]),
                 ("migrate", ["C:\\old"], ["old_dir", "verbose"])]
        for cmd, extra, attrs in cases:
            a = self.parse(cmd, *extra)
            self.assertTrue(callable(a.handler), cmd)
            for attr in attrs:
                self.assertTrue(hasattr(a, attr), f"`{cmd}` is missing attribute '{attr}'")

    def test_values_and_defaults(self):
        self.assertEqual(self.parse("enroll").samples, 5)
        self.assertEqual(self.parse("enroll", "--samples", "4").samples, 4)
        self.assertEqual(self.parse("voice-test").seconds, 30.0)
        self.assertEqual(self.parse("voice-test", "--seconds", "7.5").seconds, 7.5)
        self.assertEqual(self.parse("run", "--port", "9100").port, 9100)

    def test_bad_input_exits_2_with_a_message(self):
        for argv in (["enroll", "--samples", "9"], ["enroll", "--samples", "x"], ["voice-test", "--seconds", "abc"],
                     ["migrate"], ["bogus"], ["run", "--window", "x"], ["run", "--nonsense"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                self.parse(*argv)
            self.assertEqual(cm.exception.code, 2, argv)
            self.assertIn("usage", err.getvalue().lower())


class DispatchTests(unittest.TestCase):
    """Calls every real handler with external effects mocked: any missing attribute would raise AttributeError here."""

    def go(self, *argv):
        args = alpha_main.build_parser().parse_args(alpha_main.normalize_argv(list(argv)))
        return args.handler(args)

    def test_voice_commands_forward_exactly_their_own_options(self):
        with mock.patch("voice_security.voice_auth.main", return_value=0) as m:
            self.go("enroll"); self.assertEqual(m.call_args.args[0], ["enroll", "--samples", "5"])
            self.go("enroll", "--samples", "3", "--impostor"); self.assertEqual(m.call_args.args[0], ["enroll", "--samples", "3", "--impostor"])
            self.go("voice-test"); self.assertEqual(m.call_args.args[0], ["test", "--seconds", "30.0"])
            self.go("voice-test", "--seconds", "5"); self.assertEqual(m.call_args.args[0], ["test", "--seconds", "5.0"])
            self.go("voice-status"); self.assertEqual(m.call_args.args[0], ["status"])
            self.go("voice-reset"); self.assertEqual(m.call_args.args[0], ["reset"])

    def test_other_commands(self):
        with mock.patch("voice.engine.run_mic_test") as m:
            self.assertEqual(self.go("mic-test"), 0); m.assert_called_once()
        fake = mock.MagicMock(); fake.query_devices.return_value = "DEVICES"
        with mock.patch.dict(sys.modules, {"sounddevice": fake}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.go("list-mics"), 0)
        self.assertIn("DEVICES", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.go("tools"), 0)
        self.assertIn("System Control", out.getvalue())
        with mock.patch("scripts.migrate_old_project.migrate", return_value=0) as m:
            self.go("migrate", "C:\\old"); m.assert_called_once_with("C:\\old")
        with mock.patch("scripts.doctor.run", return_value=0) as m:
            self.assertEqual(self.go("doctor"), 0)

    def test_run_wires_lock_assistant_and_dashboard(self):
        alpha = mock.MagicMock()
        with mock.patch("core.assistant.Alpha", return_value=alpha), mock.patch("core.instance.InstanceLock") as lock, \
                mock.patch.object(alpha_main, "start_dashboard", return_value=mock.MagicMock()) as ui:
            self.assertEqual(self.go("run", "--text", "--mute"), 0)
        lock.return_value.acquire.assert_called_once(); alpha.run.assert_called_once(); alpha.shutdown.assert_called_once()
        ui.assert_called_once(); lock.return_value.release.assert_called_once()
        with mock.patch("core.assistant.Alpha", return_value=alpha), mock.patch("core.instance.InstanceLock"), \
                mock.patch.object(alpha_main, "start_dashboard") as ui:
            self.go("run", "--no-ui"); ui.assert_not_called()


class SubprocessTests(unittest.TestCase):
    """Real `python main.py ...` invocations."""

    def clean(self, rc_out, allowed=(0,)):
        rc, out, data = rc_out
        self.addCleanup(shutil.rmtree, data, True)
        for bad in ("Traceback", "AttributeError", "ModuleNotFoundError", "NameError"):
            self.assertNotIn(bad, out, out)
        self.assertIn(rc, allowed, out)
        return out

    def test_help_for_every_command(self):
        self.clean(run_main("--help"))
        for cmd in alpha_main.COMMANDS:
            out = self.clean(run_main(cmd, "--help"))
            self.assertIn("usage", out.lower(), cmd)

    def test_commands_that_need_no_hardware(self):
        self.clean(run_main("voice-status"))
        self.clean(run_main("voice-reset"))
        self.assertIn("Alpha AI doctor", self.clean(run_main("doctor"), allowed=(0, 1)))
        self.assertIn("Memory & Notes", self.clean(run_main("tools")))
        self.clean(run_main("-v", "voice-status"))                      # option before the command

    def test_hardware_commands_fail_gracefully_not_with_a_traceback(self):
        for argv in (["list-mics"], ["mic-test"], ["voice-test", "--seconds", "1"], ["enroll", "--samples", "3"]):
            rc, out, data = run_main(*argv, timeout=90); self.addCleanup(shutil.rmtree, data, True)
            self.assertNotIn("AttributeError", out, argv); self.assertNotIn("Traceback", out, argv)

    def test_migrate_with_a_real_folder(self):
        old = Path(tempfile.mkdtemp(prefix="old_alpha_")); self.addCleanup(shutil.rmtree, old, True)
        (old / ".env").write_text("GEMINI_API_KEY=AIzaTEST\n")
        out = self.clean(run_main("migrate", str(old)))
        self.assertIn("Migrating from", out)
        self.assertEqual(run_main("migrate", str(old / "missing"))[0], 1)

    def test_run_text_mode_clean_start_and_exit(self):
        rc, out, data = run_main("run", "--text", "--no-ui", "--mute", "--no-tracking", stdin="take a note: hello\ngoodbye\n")
        self.addCleanup(shutil.rmtree, data, True)
        self.assertEqual(rc, 0, out); self.assertIn("Goodbye", out); self.assertIn("Noted", out)
        self.assertFalse((Path(data) / "alpha.lock").exists(), "lock file left behind after exit")
        rc, out, data2 = run_main("--text", "--no-ui", "--mute", "--no-tracking", stdin="goodbye\n")   # bare options mean `run`
        self.addCleanup(shutil.rmtree, data2, True); self.assertEqual(rc, 0, out)

    def test_second_instance_is_refused_with_a_clear_message(self):
        data = tempfile.mkdtemp(prefix="alpha_lock_"); self.addCleanup(shutil.rmtree, data, True)
        (Path(data) / "alpha.lock").write_text(str(os.getpid()))        # a live python process (this one) holds the lock
        rc, out, _ = run_main("run", "--text", "--no-ui", "--mute", env_extra={"ALPHA_DATA_DIR": data})
        self.assertEqual(rc, 2, out); self.assertIn("already running", out.lower())
        (Path(data) / "alpha.lock").write_text("999999")                # a stale pid does not block
        rc, out, _ = run_main("run", "--text", "--no-ui", "--mute", "--no-tracking", stdin="goodbye\n", env_extra={"ALPHA_DATA_DIR": data})
        self.assertEqual(rc, 0, out)


class NestingTrapTests(unittest.TestCase):
    def test_project_one_folder_too_deep_is_detected_and_repaired(self):
        outer = Path(tempfile.mkdtemp(prefix="nest_")); self.addCleanup(shutil.rmtree, outer, True)
        inner = outer / "AlphaAIv4"; inner.mkdir(); copy_project(inner)
        shutil.copy2(ROOT / "main.py", outer / "main.py")              # main.py in the outer folder, packages one level down
        rc, out, data = run_main("tools", cwd=outer, main_path=outer / "main.py"); self.addCleanup(shutil.rmtree, data, True)
        self.assertEqual(rc, 0, out)
        self.assertIn("one folder too deep", out); self.assertIn("System Control", out)
        self.assertNotIn("ModuleNotFoundError", out)

    def test_missing_folders_give_a_precise_fix_not_a_traceback(self):
        lonely = Path(tempfile.mkdtemp(prefix="lonely_")); self.addCleanup(shutil.rmtree, lonely, True)
        shutil.copy2(ROOT / "main.py", lonely / "main.py")
        rc, out, data = run_main("tools", cwd=lonely, main_path=lonely / "main.py"); self.addCleanup(shutil.rmtree, data, True)
        self.assertEqual(rc, 3, out)
        self.assertIn("side by side in ONE", out); self.assertIn("config", out); self.assertNotIn("Traceback", out)

    def test_runs_from_any_working_directory(self):
        here = Path(tempfile.mkdtemp(prefix="cwd_")); self.addCleanup(shutil.rmtree, here, True)
        rc, out, data = run_main("tools", cwd=here); self.addCleanup(shutil.rmtree, data, True)
        self.assertEqual(rc, 0, out); self.assertNotIn("ModuleNotFoundError", out)


if __name__ == "__main__":
    unittest.main()
