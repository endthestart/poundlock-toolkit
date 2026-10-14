import { posix } from "node:path";

// Read-only fixture policy loaded explicitly; model tools cannot edit this file.
// Substitution values are embedded as JSON literals by pi.py from the selected
// PiFixture, so this one policy covers every fixture (coding, investigation).
export default function (pi) {
  let requests = 0;
  const verifier = __VERIFIER_COMMAND_JSON__;
  const allowedWrite = __ALLOWED_WRITE_JSON__;
  const readablePrefixes = __READABLE_PREFIXES_JSON__;
  // Empty string unless the compaction gate is on. Re-asserted in the system
  // role on every request rather than relied on from the first user turn,
  // because compaction summarizes conversation history but never the system
  // prompt: this is the fix found in long-run use for a summarized-away report format.
  const reportContract = __REPORT_CONTRACT_JSON__;
  pi.on("tool_call", async (event) => {
    const input = event.input;
    let allowed = false;
    if (input && event.toolName === "bash") {
      allowed = verifier !== "" && input.command === verifier;
    } else if (input && typeof input.path === "string") {
      const path = posix.resolve("/work", input.path);
      const writable = allowedWrite.includes(path);
      const readable = readablePrefixes.some((p) => path === p || path.startsWith(p));
      allowed = ((event.toolName === "write" || event.toolName === "edit") && writable)
        || (event.toolName === "read" && (writable || readable));
    }
    if (!allowed) return { block: true, reason: "This fixture allows only its declared task outputs and, when configured, the exact protected verifier command." };
  });
  pi.on("before_provider_request", async (event) => {
    if (++requests > 12) process.exit(43);
    let messages = event.payload.messages;
    if (reportContract && Array.isArray(messages)) {
      if (messages.length && messages[0].role === "system") {
        messages = [
          { ...messages[0], content: `${messages[0].content}\n\n${reportContract}` },
          ...messages.slice(1),
        ];
      } else {
        messages = [{ role: "system", content: reportContract }, ...messages];
      }
    }
    return { ...event.payload, messages, temperature: __TEMPERATURE__, max_tokens: __MAX_OUTPUT__ };
  });
}
