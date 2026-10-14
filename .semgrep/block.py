# Positive and negative controls for block.yml. A rule that never fires would
# make the gate look clean, so CI runs `semgrep --test .semgrep` before the scan.
import hashlib
import os
import pickle
import subprocess
import tarfile
import tempfile
import urllib.request

import yaml


def shell(cmd, argv):
    # ruleid: toolkit-py-subprocess-shell
    subprocess.run(cmd, shell=True)
    # ruleid: toolkit-py-subprocess-shell
    os.system(cmd)
    # ok: toolkit-py-subprocess-shell
    subprocess.run(argv, shell=False)


def dash_c(cmd):
    # ruleid: toolkit-py-shell-dash-c
    return ["bash", "-c", cmd]


def dynamic(data, text):
    # ruleid: toolkit-py-dynamic-code
    eval(text)
    # ruleid: toolkit-py-dynamic-code
    pickle.loads(data)
    # ruleid: toolkit-py-dynamic-code
    yaml.load(text)
    # ok: toolkit-py-dynamic-code
    yaml.safe_load(text)


def extract(path, dest):
    with tarfile.open(path) as archive:
        # ruleid: toolkit-py-archive-extract-without-filter
        archive.extractall(dest)
        # ok: toolkit-py-archive-extract-without-filter
        archive.extractall(dest, filter="data")


def tls(ctx, requests_get, url):
    # ruleid: toolkit-py-tls-verification-off
    requests_get(url, verify=False)
    # ruleid: toolkit-py-tls-verification-off
    ctx.check_hostname = False


def hashes(data):
    # ruleid: toolkit-py-weak-hash
    hashlib.md5(data)
    # ok: toolkit-py-weak-hash
    hashlib.md5(data, usedforsecurity=False)
    # ok: toolkit-py-weak-hash
    hashlib.sha256(data)


def temp():
    # ruleid: toolkit-py-tempfile-mktemp
    return tempfile.mktemp()


def fetch(url):
    # ruleid: toolkit-py-urlopen-dynamic-url
    urllib.request.urlopen(url)
    # ok: toolkit-py-urlopen-dynamic-url
    urllib.request.urlopen("https://example.com/")


# ruleid: toolkit-py-plain-http-literal
ENDPOINT = "http://controller.example:7331/v1"
# ok: toolkit-py-plain-http-literal
LOOPBACK = "http://127.0.0.1:8080/v1"
# ok: toolkit-py-plain-http-literal
SECURE = "https://controller.example/v1"
