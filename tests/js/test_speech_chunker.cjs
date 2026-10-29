/**
 * Test suite for the SpeechChunker narration chunker.
 *
 * Runs on plain Node.js with zero dependencies:
 *
 *   node tests/js/test_speech_chunker.cjs
 *
 * The reporting mirrors tests/_runner.py: one PASS/FAIL line per test
 * and a non-zero exit code when anything fails.
 */
"use strict";

const assert = require("assert");
const SpeechChunker = require("../../webui/js/speech_chunker.js");

const tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

/** Collapse whitespace runs for content-preservation comparisons. */
function normalize(text) {
  return text.replace(/\s+/g, " ").trim();
}

/**
 * Assert the packing invariants on a chunk list: nothing empty, no
 * stray whitespace, order and content preserved, and every chunk within
 * the size cap unless it holds a single word longer than the cap.
 */
function assertPacking(chunks, source, maxChars) {
  assert.ok(chunks.length > 0, "expected at least one chunk");
  for (const chunk of chunks) {
    assert.strictEqual(chunk, chunk.trim(), "no stray whitespace");
    assert.ok(chunk.length > 0, "no empty chunks");
    const words = chunk.split(" ");
    const oversizedWord = words.some((word) => word.length > maxChars);
    assert.ok(
      oversizedWord || chunk.length <= maxChars,
      "chunk " + chunk.length + " > " + maxChars + ": " + chunk.slice(0, 60),
    );
  }
  assert.strictEqual(chunks.join(" "), normalize(source));
}

/* ------------------------------------------------------------------ */
/* ICU sentence battery: English                                        */
/* ------------------------------------------------------------------ */

test("icu en: title abbreviation before a name is merged", () => {
  const out = SpeechChunker.splitSentences(
    "She met Dr. Chen last week. The draft grew by 3.5 pages.",
  );
  assert.deepStrictEqual(out, [
    "She met Dr. Chen last week.",
    "The draft grew by 3.5 pages.",
  ]);
});

test("icu en: decimals and currency stay inside sentences", () => {
  const out = SpeechChunker.splitSentences(
    "The file cost $3.50. It shipped on Jan. 5 anyway.",
  );
  assert.deepStrictEqual(out, [
    "The file cost $3.50.",
    "It shipped on Jan. 5 anyway.",
  ]);
});

test("icu en: inline enumerations are not split", () => {
  const out = SpeechChunker.splitSentences(
    "1. buy milk 2. sell milk 3. profit",
  );
  assert.deepStrictEqual(out, ["1. buy milk 2. sell milk 3. profit"]);
});

test("icu en: a quoted sentence boundary is honored", () => {
  const out = SpeechChunker.splitSentences('He said "stop." Then he left.');
  assert.deepStrictEqual(out, ['He said "stop."', "Then he left."]);
});

test("icu en: ellipses do not force a boundary", () => {
  const out = SpeechChunker.splitSentences("Wait... what? Really?! Yes.");
  assert.deepStrictEqual(out, ["Wait... what?", "Really?!", "Yes."]);
});

test("icu en: version numbers and domain names survive", () => {
  const out = SpeechChunker.splitSentences(
    "Version 2.5.1 fixes bugs. Visit example.com. Then leave.",
  );
  assert.deepStrictEqual(out, [
    "Version 2.5.1 fixes bugs.",
    "Visit example.com.",
    "Then leave.",
  ]);
});

test("icu en: time abbreviations like 9 a.m. stay inside", () => {
  const out = SpeechChunker.splitSentences(
    "We deployed at 9 a.m. Nobody noticed.",
  );
  assert.deepStrictEqual(out, ["We deployed at 9 a.m.", "Nobody noticed."]);
});

/* ------------------------------------------------------------------ */
/* ICU sentence battery: Spanish                                        */
/* ------------------------------------------------------------------ */

test("icu es: El Dr. García is merged across the title", () => {
  const source = "El Dr. García llegó a las 9 a. m. La reunión empezó tarde.";
  const out = SpeechChunker.splitSentences(source);
  assert.strictEqual(out.join(" "), normalize(source));
  for (const segment of out) {
    assert.ok(!/Dr\.$/.test(segment), "never cuts after a title");
    assert.ok(!/García/.test(segment) || segment.includes("El Dr. García"));
  }
  /* Chromium's ICU splits after "a. m."; Node's ICU suppresses it and
     over-merges. Both are correct here: a missed boundary only merges
     two sentences, which the chunk packer rejoins anyway. */
  assert.ok(out.length >= 1 && out.length <= 2);
  assert.ok(out[out.length - 1].endsWith("tarde."));
});

test("icu es: EE. UU. is merged into one sentence", () => {
  const out = SpeechChunker.splitSentences(
    "EE. UU. anunció nuevas medidas. ¿Qué pasará?",
  );
  assert.deepStrictEqual(out, [
    "EE. UU. anunció nuevas medidas.",
    "¿Qué pasará?",
  ]);
});

test("icu es: inline enumerations are not split", () => {
  const out = SpeechChunker.splitSentences("1. comprar leche 2. vender leche");
  assert.deepStrictEqual(out, ["1. comprar leche 2. vender leche"]);
});

test("icu es: pág. before a number stays inside the sentence", () => {
  const out = SpeechChunker.splitSentences(
    "Esto cuesta 3.50 euros. Véase la pág. 12 del libro.",
  );
  assert.deepStrictEqual(out, [
    "Esto cuesta 3.50 euros.",
    "Véase la pág. 12 del libro.",
  ]);
});

test("icu es: Sr., Sra. and vs. merge into one sentence", () => {
  const out = SpeechChunker.splitSentences(
    "El Sr. López y la Sra. Martínez vs. el ayuntamiento llegaron a un acuerdo.",
  );
  assert.deepStrictEqual(out, [
    "El Sr. López y la Sra. Martínez vs. el ayuntamiento llegaron a un acuerdo.",
  ]);
});

test("icu es: p. ej. stays inside the sentence", () => {
  const out = SpeechChunker.splitSentences(
    "Usa frutas, p. ej. manzanas. Y verduras.",
  );
  assert.deepStrictEqual(out, ["Usa frutas, p. ej. manzanas.", "Y verduras."]);
});

test("icu es: etc. remains a real sentence boundary", () => {
  const out = SpeechChunker.splitSentences(
    "Compré pan, leche, etc. También fruta.",
  );
  assert.deepStrictEqual(out, ["Compré pan, leche, etc.", "También fruta."]);
});

test("icu es: news prose splits at every real boundary", () => {
  const out = SpeechChunker.splitSentences(
    "Al menos 68 muertos en Urda (Toledo). El ministro de Transportes, Óscar Puente, calificó el suceso de inexplicable. La Guardia Civil interrogó al operador.",
  );
  assert.deepStrictEqual(out, [
    "Al menos 68 muertos en Urda (Toledo).",
    "El ministro de Transportes, Óscar Puente, calificó el suceso de inexplicable.",
    "La Guardia Civil interrogó al operador.",
  ]);
});

/* ------------------------------------------------------------------ */
/* Merge list unit behavior                                             */
/* ------------------------------------------------------------------ */

test("merge: chains several title fragments in a row", () => {
  const out = SpeechChunker.splitSentences(
    "El Dr. García y la Sra. López llegaron. Empezó la reunión.",
  );
  assert.deepStrictEqual(out, [
    "El Dr. García y la Sra. López llegaron.",
    "Empezó la reunión.",
  ]);
});

test("merge: leaves ordinary sentences untouched", () => {
  const out = SpeechChunker.splitSentences("First one here. Second one here.");
  assert.deepStrictEqual(out, ["First one here.", "Second one here."]);
});

test("merge: works with CRLF and hard paragraph breaks", () => {
  const out = SpeechChunker.splitSentences(
    "El Dr. García llegó.\r\n\r\nLa reunión empezó tarde.",
  );
  assert.deepStrictEqual(out, [
    "El Dr. García llegó.",
    "La reunión empezó tarde.",
  ]);
});

/* ------------------------------------------------------------------ */
/* Fallback path (no Intl.Segmenter)                                    */
/* ------------------------------------------------------------------ */

test("fallback: still merges title abbreviations", () => {
  const out = SpeechChunker.splitSentences(
    "She met Dr. Chen last week. The draft grew by 3.5 pages.",
    { useSegmenter: false },
  );
  assert.deepStrictEqual(out, [
    "She met Dr. Chen last week.",
    "The draft grew by 3.5 pages.",
  ]);
});

test("fallback: decimals are never boundaries", () => {
  const out = SpeechChunker.splitSentences(
    "The value is 3.14159. Round it up.",
    { useSegmenter: false },
  );
  assert.deepStrictEqual(out, ["The value is 3.14159.", "Round it up."]);
});

test("fallback: splits on ? ! and ellipses", () => {
  const out = SpeechChunker.splitSentences("Really?! Yes. Wait... ok.", {
    useSegmenter: false,
  });
  assert.deepStrictEqual(out, ["Really?!", "Yes.", "Wait...", "ok."]);
});

test("fallback: inline enumerations split (documented weakness)", () => {
  const out = SpeechChunker.splitSentences("1. buy milk 2. sell milk", {
    useSegmenter: false,
  });
  assert.deepStrictEqual(out, ["1.", "buy milk 2.", "sell milk"]);
});

/* ------------------------------------------------------------------ */
/* Packing                                                              */
/* ------------------------------------------------------------------ */

test("pack: honors a small maxChars at sentence boundaries", () => {
  const source =
    "Al menos 68 muertos en Urda. El ministro habló de inexplicable. La investigación continúa.";
  const chunks = SpeechChunker.split(source, { maxChars: 45 });
  assertPacking(chunks, source, 45);
  assert.deepStrictEqual(chunks, [
    "Al menos 68 muertos en Urda.",
    "El ministro habló de inexplicable.",
    "La investigación continúa.",
  ]);
});

test("pack: greedy fill puts several sentences in one chunk", () => {
  const source = "One. Two. Three.";
  const chunks = SpeechChunker.split(source, { maxChars: 20 });
  assert.deepStrictEqual(chunks, ["One. Two. Three."]);
});

test("pack: a long sentence is hard-split at word boundaries", () => {
  const source =
    "El accidente ferroviario de la línea de alta velocidad Madrid Sevilla dejó decenas de viajeros atrapados dentro de los vagones descarrilados durante horas.";
  const chunks = SpeechChunker.split(source, { maxChars: 40 });
  assertPacking(chunks, source, 40);
  for (const chunk of chunks) {
    assert.ok(chunk.split(" ").length > 1, "never cuts mid-word");
  }
});

test("pack: an oversized sentence keeps its terminator on the last piece", () => {
  const source = "palabra ".repeat(30).trim() + " final.";
  const chunks = SpeechChunker.split(source, { maxChars: 50 });
  assertPacking(chunks, source, 50);
  assert.ok(chunks[chunks.length - 1].endsWith("final."));
});

test("pack: a single word longer than maxChars gets its own chunk", () => {
  const source = "Start. " + "x".repeat(120) + " End.";
  const chunks = SpeechChunker.split(source, { maxChars: 50 });
  assert.ok(chunks.some((chunk) => chunk === "x".repeat(120)));
  assertPacking(chunks, source, 50);
});

test("pack: paragraphs become chunk boundaries when they overflow", () => {
  const source =
    "Primer párrafo con varias palabras aquí. Segundo párrafo con más texto todavía dentro.";
  const chunks = SpeechChunker.split(source, { maxChars: 45 });
  assertPacking(chunks, source, 45);
  assert.strictEqual(chunks[0], "Primer párrafo con varias palabras aquí.");
});

/* ------------------------------------------------------------------ */
/* Degenerate inputs                                                    */
/* ------------------------------------------------------------------ */

test("empty: empty string yields no chunks", () => {
  assert.deepStrictEqual(SpeechChunker.split(""), []);
});

test("empty: whitespace-only text yields no chunks", () => {
  assert.deepStrictEqual(SpeechChunker.split("   \n\n  \t "), []);
});

test("empty: a lone word becomes one chunk", () => {
  assert.deepStrictEqual(SpeechChunker.split("Hola"), ["Hola"]);
});

test("empty: text without terminators stays whole", () => {
  const source = "una línea sin ningún signo de puntuación final";
  assert.deepStrictEqual(SpeechChunker.split(source), [source]);
});

test("empty: only the paragraph separator yields no chunks", () => {
  assert.deepStrictEqual(SpeechChunker.split("\n\n\n"), []);
});

/* ------------------------------------------------------------------ */
/* Content fidelity                                                     */
/* ------------------------------------------------------------------ */

test("fidelity: emoji, html and sentinels pass through untouched", () => {
  const source = "El país 🇪🇸 celebra. <script>alert(1)</script> Termina aquí.";
  const out = SpeechChunker.splitSentences(source);
  assert.strictEqual(out.join(" "), normalize(source));
});

test("fidelity: accents and symbols survive packing", () => {
  const source = "Costó 50€, ¿no? ¡Sí! Terminó «bien» — más o menos.";
  const chunks = SpeechChunker.split(source, { maxChars: 30 });
  assertPacking(chunks, source, 30);
});

test("fidelity: quotes attached to terminators are preserved", () => {
  const out = SpeechChunker.splitSentences('Dijo "basta". Y se fue.');
  assert.deepStrictEqual(out, ['Dijo "basta".', "Y se fue."]);
});

/* ------------------------------------------------------------------ */
/* Real-shaped narration fixture (the failure that motivated this)       */
/* ------------------------------------------------------------------ */

const NEWS_PARAGRAPH =
  "Al menos 68 muertos y 179 heridos en la colisión de dos trenes de alta velocidad en Urda (Toledo), en la línea Madrid-Sevilla. " +
  "El ministro de Transportes, Óscar Puente, calificó el suceso de inexplicable y atribuyó el choque a un error humano en el centro de control. " +
  "El Dr. Sánchez dijo que es inasumible y la Guardia Civil ya ha interrogado al operador del centro de control. " +
  "La jornada de Copa del Rey fue suspendida y el deporte español se tiñe de luto. ";

test("fixture: a 6000-char news reply packs into small ordered chunks", () => {
  const source = NEWS_PARAGRAPH.repeat(14);
  assert.ok(source.length > 5500, "fixture mirrors the failed reply size");
  const started = performance.now();
  const chunks = SpeechChunker.split(source);
  const elapsed = performance.now() - started;
  assertPacking(chunks, source, SpeechChunker.DEFAULT_MAX_CHARS);
  assert.ok(
    chunks.length >= 5,
    "expected several chunks, got " + chunks.length,
  );
  assert.ok(elapsed < 100, "split took " + elapsed.toFixed(1) + "ms");
  assert.ok(chunks[0].startsWith("Al menos 68 muertos"));
  assert.ok(chunks[0].length <= SpeechChunker.DEFAULT_MAX_CHARS);
});

test("fixture: split is deterministic", () => {
  const source = NEWS_PARAGRAPH.repeat(3);
  assert.deepStrictEqual(
    SpeechChunker.split(source),
    SpeechChunker.split(source),
  );
});

/* ------------------------------------------------------------------ */
/* API contract                                                         */
/* ------------------------------------------------------------------ */

test("api: default maxChars is 1000 and exported", () => {
  assert.strictEqual(SpeechChunker.DEFAULT_MAX_CHARS, 1000);
  const longSentence = "palabra ".repeat(300).trim();
  const chunks = SpeechChunker.split(longSentence);
  for (const chunk of chunks) {
    assert.ok(chunk.length <= 1000 || chunk.split(" ").length === 1);
  }
});

test("api: invalid options fall back to the defaults", () => {
  const source = "Primera frase aquí. Segunda frase allí. Tercera frase ahora.";
  for (const bad of [
    { maxChars: 0 },
    { maxChars: -5 },
    { maxChars: "x" },
    null,
  ]) {
    const chunks = SpeechChunker.split(source, bad);
    assertPacking(chunks, source, SpeechChunker.DEFAULT_MAX_CHARS);
  }
});

test("api: splitSentences exposes the intermediate segments", () => {
  const out = SpeechChunker.splitSentences("Uno. Dos.");
  assert.deepStrictEqual(out, ["Uno.", "Dos."]);
});

test("api: the fallback and ICU paths agree on the title battery", () => {
  const source = "She met Dr. Chen. He left.";
  assert.deepStrictEqual(
    SpeechChunker.splitSentences(source),
    SpeechChunker.splitSentences(source, { useSegmenter: false }),
  );
});

/* ------------------------------------------------------------------ */
/* Runner                                                               */
/* ------------------------------------------------------------------ */

let failed = [];
for (const { name, fn } of tests) {
  try {
    fn();
    console.log("PASS " + name);
  } catch (err) {
    failed.push(name);
    console.log("FAIL " + name);
    console.log(err && err.stack ? err.stack : String(err));
  }
}
if (failed.length) {
  console.log(failed.length + " TEST(S) FAILED: " + failed.join(", "));
  process.exit(1);
}
console.log("ALL " + tests.length + " TESTS PASSED");
