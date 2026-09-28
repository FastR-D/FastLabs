export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[char]);
}

export function formatLocalTime(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(source) ? source : `${source}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return source;
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function logPresentation(event) {
  const message = String(event?.message || "");
  const embedded = message.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+([\s\S]*)$/);
  const sourceTime = embedded ? embedded[1] : event?.created_at;
  return {
    sourceTime,
    time: formatLocalTime(sourceTime),
    message: embedded ? embedded[2] : message,
  };
}

export function visibleLogEvents(events = []) {
  return events.filter((event) => {
    const kind = String(event.kind || "").trim();
    const message = String(event.message || "").trim();
    if (!message) return false;
    const leaf = kind.split(".").pop();
    return message !== kind && message !== leaf && !["user", "assistant"].includes(message);
  });
}

export function plannerLabel(planner = {}) {
  return planner.backend === "claude" ? "Claude CLI" : "Codex CLI";
}
