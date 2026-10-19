/** Text comparison used to decide whether a human "changed" a draft. */

export function normalizeForCompare(text: string): string {
  return text.replace(/\r\n/g, "\n").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
}

function words(text: string): string[] {
  return normalizeForCompare(text).split(/\s+/).filter(Boolean);
}

/** Word-level Levenshtein distance (two-row DP). */
export function wordEditDistance(a: string, b: string): number {
  const x = words(a);
  const y = words(b);
  let prev = Array.from({ length: y.length + 1 }, (_, j) => j);
  for (let i = 1; i <= x.length; i++) {
    const cur = [i];
    for (let j = 1; j <= y.length; j++) {
      const cost = x[i - 1] === y[j - 1] ? 0 : 1;
      cur[j] = Math.min((prev[j] ?? 0) + 1, (cur[j - 1] ?? 0) + 1, (prev[j - 1] ?? 0) + cost);
    }
    prev = cur;
  }
  return prev[y.length] ?? 0;
}

/** 0 = identical, 1 = completely rewritten. */
export function editRatio(original: string, final: string): number {
  const denom = Math.max(words(original).length, words(final).length, 1);
  return Math.round((wordEditDistance(original, final) / denom) * 1000) / 1000;
}

/**
 * "Without changing" = identical after whitespace normalisation. Deliberately strict: a one-word edit
 * ("can" -> "cannot") can flip the meaning of a support reply, so any word change counts as a change.
 */
export function isUnchanged(original: string, final: string): boolean {
  return normalizeForCompare(original) === normalizeForCompare(final);
}
