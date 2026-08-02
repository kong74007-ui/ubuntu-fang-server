const DEFAULT_LIMITS = Object.freeze({
  maxBytes: 512 * 1024,
  maxDepth: 24,
  maxItems: 5000,
  maxStringChars: 4000,
});
const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);


class StrictParser {
  constructor(text, limits) {
    this.text = text;
    this.limits = limits;
    this.index = 0;
    this.items = 0;
  }

  error(code) { throw new Error(code); }
  space() { while (/[\x20\t\r\n]/.test(this.text[this.index] || "")) this.index += 1; }

  string() {
    const start = this.index;
    if (this.text[this.index++] !== '"') this.error("json_string_invalid");
    let escaped = false;
    while (this.index < this.text.length) {
      const character = this.text[this.index++];
      if (escaped) { escaped = false; continue; }
      if (character === "\\") { escaped = true; continue; }
      if (character === '"') {
        let value;
        try { value = JSON.parse(this.text.slice(start, this.index)); } catch { this.error("json_string_invalid"); }
        if (value.length > this.limits.maxStringChars) this.error("json_string_exceeded");
        if ([...value].some((char) => char < " " || /[\uD800-\uDFFF]/u.test(char))) this.error("json_string_invalid");
        return value;
      }
      if (character < " ") this.error("json_string_invalid");
    }
    this.error("json_string_invalid");
  }

  value(depth = 1) {
    if (depth > this.limits.maxDepth) this.error("json_depth_exceeded");
    this.space();
    const character = this.text[this.index];
    if (character === "{") return this.object(depth);
    if (character === "[") return this.array(depth);
    if (character === '"') return this.string();
    for (const [literal, value] of [["true", true], ["false", false], ["null", null]]) {
      if (this.text.startsWith(literal, this.index)) { this.index += literal.length; return value; }
    }
    const match = this.text.slice(this.index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (match) {
      this.index += match[0].length;
      const number = Number(match[0]);
      if (!Number.isFinite(number)) this.error("json_nonfinite_number");
      return number;
    }
    this.error(/^(?:NaN|Infinity|-Infinity)/.test(this.text.slice(this.index)) ? "json_nonfinite_number" : "json_invalid");
  }

  object(depth) {
    this.index += 1;
    const result = Object.create(null);
    const seen = new Set();
    this.space();
    if (this.text[this.index] === "}") { this.index += 1; return Object.freeze(result); }
    for (;;) {
      this.space();
      if (this.text[this.index] !== '"') this.error("json_object_key_invalid");
      const key = this.string();
      if (FORBIDDEN_KEYS.has(key)) this.error("json_prototype_key_forbidden");
      if (seen.has(key)) this.error("json_duplicate_key");
      seen.add(key);
      this.items += 1;
      if (this.items > this.limits.maxItems) this.error("json_items_exceeded");
      this.space();
      if (this.text[this.index++] !== ":") this.error("json_invalid");
      result[key] = this.value(depth + 1);
      this.space();
      const next = this.text[this.index++];
      if (next === "}") return Object.freeze(result);
      if (next !== ",") this.error("json_invalid");
    }
  }

  array(depth) {
    this.index += 1;
    const result = [];
    this.space();
    if (this.text[this.index] === "]") { this.index += 1; return Object.freeze(result); }
    for (;;) {
      this.items += 1;
      if (this.items > this.limits.maxItems) this.error("json_items_exceeded");
      result.push(this.value(depth + 1));
      this.space();
      const next = this.text[this.index++];
      if (next === "]") return Object.freeze(result);
      if (next !== ",") this.error("json_invalid");
    }
  }
}


export function parseCanonicalJson(bytes, limits = {}) {
  if (!Buffer.isBuffer(bytes) && !(bytes instanceof Uint8Array)) throw new Error("json_input_invalid");
  const merged = {...DEFAULT_LIMITS, ...limits};
  if (bytes.byteLength > merged.maxBytes) throw new Error("json_bytes_exceeded");
  let text;
  try { text = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes); } catch { throw new Error("json_utf8_invalid"); }
  if (text.startsWith("\uFEFF")) throw new Error("json_bom_forbidden");
  const parser = new StrictParser(text, merged);
  const value = parser.value(1);
  parser.space();
  if (parser.index !== text.length) throw new Error("json_trailing_content");
  return value;
}
