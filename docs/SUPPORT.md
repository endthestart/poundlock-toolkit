# Support matrix

Reviewed 2026-09-13. Upstream platform claims are prerequisites, not Toolkit test
results. Toolkit never installs drivers, tunes hosts or downloads model catalogs.

| Layer | First-release scope | Evidence level |
|---|---|---|
| Python CLI | Linux and macOS, Python >=3.11 | Tests and clean wheel/sdist installs run in Linux CI and locally on macOS |
| Generic OpenAI API | `/models`, chat completion, streamed content/usage, forced tool calls | Offline wire tests; live endpoint authentication gate documented separately |
| Native Ollama API | `/api/tags`, bounded nonstream `/api/chat`, native timing fields | Second configuration tested against synthetic wire server; real Ollama model unqualified |
| Pi coding | Existing pinned Docker image, Python 3, Pi 1.0.0 interface, existing internal network | Adapter and verifier checks offline in this repo's CI; separately run live in operator use against local models over multiple days, including forced-compaction and coding-under-load sessions -- that usage is outside this repo's own test suite |
| Windows | No execution deadline/isolation contract qualified | Explicitly unqualified |
| Local/SSH targets | Same installed package; finite SSH invocation | Offline transport fixtures; physical SSH model flow pending |
| AMD/Intel Linux | Bounded DRM vendor/device/driver inspection; AMD sysfs VRAM when available | Fixture-tested; Intel memory remains unknown |
| Prefix-cache probe | Opt-in, four unscored requests (prompt, repeat, agent turn, edit inside the last message); OpenAI-compatible (cached-token counts, prompt time, streamed first-token time) and native Ollama (prompt time only) | Offline wire tests against synthetic servers, including one that reuses on append but not on a tail edit; the four-request probe has not run against a live server yet |
| Memory probe | Linux: process RSS from procfs and device-wide AMD VRAM per card; macOS: process RSS from `ps`; local (loopback) servers only, including through `target-run` | macOS process sampled in tests; Linux read against fixture trees, no live Linux run yet; NVIDIA, Intel, GTT and Apple-silicon unified memory unmeasured |

Official sources and review provenance:

- **llama.cpp:** [build/install guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)
  and [backend list](https://github.com/ggml-org/llama.cpp), reviewed 2026-09-13.
  CPU builds and several vendor GPU paths exist. Choose the specific backend for
  the actual device; successful compilation does not establish model placement.
- **Ollama:** [Linux installation](https://docs.ollama.com/linux) and
  [hardware support](https://docs.ollama.com/gpu), reviewed 2026-09-13. Consult the
  exact device/driver entry; Toolkit does not mirror the full changing matrix.
- **oMLX:** [official repository and installation](https://github.com/jundot/omlx),
  reviewed 2026-09-13. Apple silicon serving is the documented scope for this entry.
- **Pi:** [official coding-agent documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
  and [custom models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md),
  reviewed 2026-09-13, and the 1.0.0 CLI flags and JSON events Toolkit reads on 2026-10-02. Pin and verify the
  actual image's version before relying on flags, hooks or sampling behavior.
- **Docker:** [container resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
  and [network create/internal isolation](https://docs.docker.com/reference/cli/docker/network/create/),
  reviewed 2026-09-13. The reviewed standalone script creates its own internal network; attached Pi
  accepts an existing one. Membership alone does not establish authority.

The planner uses a small reviewed candidate table with dates/URLs and marks
unknown inventory conservatively. Compatible upstream families still need exact
model/backend/harness/configuration tests. Old review dates should prompt a fresh
upstream check; these entries are not an exhaustive driver-support database.

Linux AMD memory fields follow the [kernel amdgpu sysfs ABI](https://docs.kernel.org/gpu/amdgpu/driver-misc.html),
reviewed 2026-09-13. NVIDIA pools come from nvidia-smi; AMD pools remain separate;
Intel memory is unknown when no reliable bound is exposed. Apple unified memory
is one shared pool, never added to system RAM. Inventory captures a point in time,
not a reservation or a guarantee that a backend actually uses that device.
