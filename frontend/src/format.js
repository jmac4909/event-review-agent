export const formatTime = (date) =>
  new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(date));

export function reviewRationale(text, modelName) {
  // Shorten only known legacy Mock templates. Exact output stays in run history.
  if (!modelName?.startsWith("deterministic-mock-")) return text;
  if (text === "The source reports a boundary crossing, but does not establish authorization.")
    return "Authorization unknown.";
  return text
    .replace(/^Mock rule: reported separation is ([\d.]+) m; the sample threshold is ([\d.]+) m\. No barrier evidence is supplied\.?$/, "$1 m separation; $2 m limit.")
    .replace(/^Reported separation is ([\d.]+) m; sample site rule \S+ sets a ([\d.]+) m threshold\. No barrier evidence is supplied\.?$/, "$1 m separation; $2 m limit.")
    .replace(/^Mock rule: the object was reported in the exit boundary for ([\d.]+) seconds; the sample threshold is ([\d.]+) seconds\.$/, "Blocked for $1s; $2s threshold.")
    .replace(/^The object was reported in the exit boundary for ([\d.]+) seconds; sample site rule \S+ sets a ([\d.]+) second threshold\.$/, "Blocked for $1s; $2s threshold.");
}
