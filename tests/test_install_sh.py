"""Tests the parts of install.sh that can be run without a server.

install.sh only runs main when it is executed, so these source it into a bash and
call one function. That is the only way to cover what Python can't see: the shell
options (set -Eeuo pipefail) that turn an unmatched grep or a failed command
substitution into a crash.

Needs PyYAML for python3, as the installer itself does.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = os.path.join(ROOT, "install.sh")

LAB = textwrap.dedent("""\
    estate_version: 1
    name: lab
    deployment: lab
    mattermost_edition: team
    mattermost_site_url: http://lab.example.test
    mattermost_version_channel: latest-esr
    tls_mode: later
    file_storage: local
    calls: none
    recording: none
    observability: none
    nodes:
      - {name: lab01, address: 192.0.2.50, profiles: [core]}
""")

# Two servers, so a server that isn't in the plan has no one to be.
TWO = LAB.replace("  - {name: lab01, address: 192.0.2.50, profiles: [core]}\n",
                  "  - {name: db01, address: 192.0.2.51, profiles: [database]}\n"
                  "  - {name: app01, address: 192.0.2.52, profiles: [app]}\n")


def find_bash():
    """A bash that has associative arrays (4 or later). macOS ships 3.2, so there
    these tests are skipped, and they run in CI and in the containers."""
    for candidate in (os.environ.get("BASH_4"), shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            major = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                                   text=True, check=False).stdout.strip()
        except OSError:
            continue
        if major.isdigit() and int(major) >= 4:
            return candidate
    return None


BASH = find_bash()


def run_bash(script, stdin="", env=None):
    """Source install.sh in a bash and run script. -> (returncode, stdout, stderr)."""
    full_env = dict(os.environ)
    # python3 -I in install.sh has to be one that has PyYAML: the one running the tests.
    full_env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + full_env["PATH"]
    full_env.update(env or {})
    proc = subprocess.run([BASH, "-c", '. "$1"; shift; ' + script, "bash", INSTALL],
                          input=stdin, capture_output=True, text=True, env=full_env, timeout=120, check=False)
    return proc.returncode, proc.stdout, proc.stderr


@unittest.skipUnless(BASH, "needs bash 4 or later")
class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def estate(self, plan, args="", stdin="", non_interactive=True):
        """Run resolve_estate against a plan, with the config directory in a sandbox."""
        plan_file = self.write("plan.yml", plan)
        script = textwrap.dedent("""\
            CONFIG_DIR="{d}"; SAVED_ANSWERS="{d}/answers.yml"; RUN_ID=test
            NON_INTERACTIVE={ni}; ESTATE="{plan}"; {args}
            resolve_estate
            echo "answers:"; cat "$SAVED_ANSWERS"
        """).format(d=self.dir, ni="true" if non_interactive else "false", plan=plan_file, args=args)
        return run_bash(script, stdin)


class ResolveEstate(Sandbox):
    def test_a_lab_with_notes_and_no_followups_gets_through(self):
        """A lab has nothing to follow up and a note to show. grep exits 1 for the
        lines it doesn't find, which pipefail made a crash at the first of them."""
        rc, out, err = self.estate(LAB, 'ESTATE_NODE=lab01')
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("stopped at line", err)
        self.assertIn("deploy_profiles", out)

    def test_a_plan_with_followups_and_no_notes_gets_through(self):
        # A provided certificate is something only you can put on the server. A
        # database-only server has no note to show.
        plan = TWO.replace("tls_mode: later", "tls_mode: provided")
        rc, out, err = self.estate(plan, 'ESTATE_NODE=app01')
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Still needed on this server", out)

    def test_a_server_that_isnt_in_the_plan_is_told_what_to_pass(self):
        """Exit code 3 is the helper asking which node this is. Unattended there is
        nobody to ask, so the answer is the option to pass, not a bare crash."""
        rc, out, err = self.estate(TWO)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("stopped at line", err)
        self.assertIn("--estate-node", err)
        self.assertIn("db01", err)
        self.assertNotIn("ERROR: ERROR", err)

    def test_a_server_that_isnt_in_the_plan_can_be_asked(self):
        rc, out, err = self.estate(TWO, stdin="2\n", non_interactive=False)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("stopped at line", err)
        self.assertIn("node app01", out)


class PastedPlan(Sandbox):
    """The plan is pasted at a prompt as one long line."""

    def big_plan(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import estate_plan  # noqa: PLC0415
        import random  # noqa: PLC0415
        rng = random.Random(7)  # names gzip can't squeeze, so the encoded line stays long
        nodes = ["  - {name: %s, address: 10.%d.%d.%d, profiles: [app]}" % (
            "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(24)),
            rng.randint(1, 250), rng.randint(1, 250), rng.randint(1, 250)) for _ in range(120)]
        plan = LAB.replace("  - {name: lab01, address: 192.0.2.50, profiles: [core]}\n", "\n".join(nodes) + "\n")
        return estate_plan.encode(plan)

    @unittest.skipUnless(hasattr(os, "fork"), "needs a pty")
    def test_a_line_longer_than_a_terminal_holds_arrives_whole(self):
        import pty  # noqa: PLC0415
        import select  # noqa: PLC0415
        import time  # noqa: PLC0415
        blob = self.big_plan()
        self.assertGreater(len(blob), 4095)
        script = ('CONFIG_DIR="%s"; ESTATE=-; NON_INTERACTIVE=false; '
                  'estate_prepare; echo "GOT=$(tr -d "\\n" < "$ESTATE_FILE" | wc -c)"; stty -a | grep -c " icanon" | sed "s/^/CANON=/"'
                  % self.dir)
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env["PATH"]
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(BASH, [BASH, "-c", '. "$1"; ' + script, "bash", INSTALL], env)
        out = b""
        sent = False
        end = time.time() + 60
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if not sent and b"press Enter" in out:
                time.sleep(0.5)
                os.write(fd, (blob + "\n").encode())
                sent = True
            if b"CANON=" in out:
                break
        os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace")
        self.assertIn("GOT=%d" % len(blob), text, text[-400:])
        # The terminal is back the way it was, so what the installer asks next can be edited.
        self.assertIn("CANON=1", text)


class FailureReason(Sandbox):
    def reason(self, text):
        path = self.write("out.txt", text)
        proc = subprocess.run([BASH, "-c", '. "$1"; failure_reason "$2"', "bash", INSTALL, path],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_a_wrapped_message_from_a_looped_assertion(self):
        text = textwrap.dedent("""\
            ok: [localhost] => (item=one)
            failed: [localhost] (item=nfs: The NFS share would be...) =>
                ansible_loop_var: item
                msg: The NFS share would be exported to nobody. List the Mattermost app servers
                    that mount it in nfs_allowed_clients.

            PLAY RECAP ****
        """)
        self.assertEqual(self.reason(text),
                         "The NFS share would be exported to nobody. List the Mattermost app servers "
                         "that mount it in nfs_allowed_clients.")

    def test_a_quoted_message_from_a_fatal_task(self):
        text = "fatal: [localhost]: FAILED! =>\n  msg: 'Went wrong: a colon'\n  rc: 1\n"
        self.assertEqual(self.reason(text), "Went wrong: a colon")

    def test_a_message_from_a_task_that_passed_is_not_a_reason(self):
        text = "ok: [localhost] => (item=x) =>\n    msg: fine\n"
        self.assertEqual(self.reason(text), "")


if __name__ == "__main__":
    unittest.main()
