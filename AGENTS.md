# Poundlock Toolkit

Toolkit discovers, measures, and recommends model setups. Keep it usable on its
own. Do not add a production controller, scheduler, model manager, or project
agent. Do not require a server database.

Keep source/config/credentials and raw evidence separate from allowlisted exports.
Preserve null metrics and every attempt. Test negative controls and independent
verification. Never infer remote memory from an inference endpoint or aggregate
separate VRAM pools into one allocation.

Inspection is read-only. Live evaluation requires a reviewed matching plan and
explicit bounded execution. Do not install drivers, tune hosts, download models,
start infrastructure or call paid services automatically. Cleanup only exact owned
resources. No unsafe host execution fallback for coding.

Use Python modules and a thin CLI. Run `python -m unittest discover -s tests -v`.
CI is offline except dependency installation and local fixture HTTP traffic. Live
hardware and Pi checks are opt-in manual gates. Toolkit-owned material is
Apache-2.0; preserve third-party licenses and source attribution.
