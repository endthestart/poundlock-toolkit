# Positive and negative controls for report.yml.
import subprocess


def spawn(argv, env):
    # ruleid: toolkit-py-subprocess-inherits-environment
    subprocess.run(argv)
    # ok: toolkit-py-subprocess-inherits-environment
    subprocess.run(argv, env=env)
