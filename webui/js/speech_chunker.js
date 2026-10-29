"use strict";

/**
 * Sentence-aware chunking of speakable text for narration.
 *
 * The speech endpoint synthesizes one passage per request and its
 * generation time grows with the passage length, so a whole reply
 * cannot be synthesized in one call: the reply's text would stay
 * hidden behind the reveal timeout and the finished audio would be
 * dropped. This module splits speakable text into ordered chunks of
 * at most ~1000 characters, cutting at sentence boundaries so every
 * request synthesizes complete sentences.
 *
 * Sentences are found with the browser's ICU-backed Intl.Segmenter,
 * which handles decimals, abbreviations like "a. m.", ellipses, quotes,
 * URLs, version numbers and inline enumerations natively, in every
 * locale. ICU's single weakness — a title abbreviation followed by a
 * capitalized word ("El Dr. García") — is repaired by merging adjacent
 * segments across a small abbreviation list. Engines without
 * Intl.Segmenter fall back to terminator scanning plus the same merge
 * list: slightly weaker on inline enumerations, never harmful.
 *
 * The module is dependency-free and DOM-free: it runs in the page and
 * under Node (module.exports) for the test suite.
 */
const SpeechChunker = (() => {
  /** Chunks target this size, roughly a minute of synthesized speech. */
  const DEFAULT_MAX_CHARS = 1000;

  /**
   * Segments ending in one of these abbreviations are joined with the
   * next segment. True sentence enders such as "etc." are excluded on
   * purpose so real boundaries survive the merge pass.
   */
  const TITLE_END =
    /(?:^|\s)(?:Dr|Dra|Mr|Mrs|Ms|Prof|Sr|Sra|Srta|Jr|St|Mt|Sgt|Cpt|Lt|Rev|Hon|Pres|Gov|Sen|Rep|EE|UU|S\.A|B\.C|A\.D|a\. m|p\. m|p\. ej|vs)\.$/i;

  const TERMINATOR_CHARS = ".!?…";
  const PARAGRAPH_BREAK = /\n{2,}/;
  const segmenters = new Map();

  /**
   * Return a cached Intl.Segmenter for the locale, or null when the
   * engine does not provide one.
   */
  function segmenterFor(locale) {
    if (typeof Intl === "undefined" || !Intl.Segmenter) return null;
    const key = locale || "";
    if (!segmenters.has(key)) {
      segmenters.set(
        key,
        new Intl.Segmenter(locale || undefined, { granularity: "sentence" }),
      );
    }
    return segmenters.get(key);
  }

  /**
   * Split text at terminator punctuation followed by whitespace,
   * keeping the punctuation with the preceding segment.
   */
  function scanTerminators(text) {
    const segments = [];
    let start = 0;
    for (let i = 0; i < text.length; i++) {
      if (!TERMINATOR_CHARS.includes(text[i])) continue;
      let end = i + 1;
      while (end < text.length && TERMINATOR_CHARS.includes(text[end])) end++;
      i = end - 1;
      if (end < text.length && /\s/.test(text[end])) {
        segments.push(text.slice(start, end));
        start = end;
      }
    }
    if (start < text.length) segments.push(text.slice(start));
    return segments;
  }

  /** Join segments ending in a title abbreviation with the next one. */
  function mergeTitleFragments(segments) {
    return segments.reduce((merged, segment) => {
      const previous = merged[merged.length - 1];
      if (previous && TITLE_END.test(previous)) {
        merged[merged.length - 1] = previous + " " + segment;
      } else {
        merged.push(segment);
      }
      return merged;
    }, []);
  }

  /** Split one paragraph into merged sentence segments. */
  function segmentSentences(paragraph, locale, useSegmenter) {
    const segmenter = useSegmenter ? segmenterFor(locale) : null;
    const raw = segmenter
      ? Array.from(segmenter.segment(paragraph), (data) => data.segment)
      : scanTerminators(paragraph);
    const trimmed = raw.map((segment) => segment.trim()).filter(Boolean);
    return mergeTitleFragments(trimmed);
  }

  /** Collect merged sentence segments across paragraphs. */
  function segmentsOf(text, options) {
    const settings = options || {};
    const locale =
      typeof settings.locale === "string" ? settings.locale : undefined;
    const useSegmenter = settings.useSegmenter !== false;
    const paragraphs = String(text || "")
      .replace(/\r\n?/g, "\n")
      .split(PARAGRAPH_BREAK);
    const sentences = [];
    for (const paragraph of paragraphs) {
      if (paragraph.trim())
        sentences.push(...segmentSentences(paragraph, locale, useSegmenter));
    }
    return sentences;
  }

  /** Break an oversized sentence at word boundaries, never mid-word. */
  function splitLongSentence(sentence, maxChars) {
    const pieces = [];
    let current = "";
    for (const word of sentence.split(" ")) {
      if (current && current.length + 1 + word.length > maxChars) {
        pieces.push(current);
        current = word;
      } else {
        current = current ? current + " " + word : word;
      }
    }
    if (current) pieces.push(current);
    return pieces;
  }

  /** Greedily pack sentences into chunks of at most maxChars characters. */
  function packIntoChunks(sentences, maxChars) {
    const chunks = [];
    let current = "";
    for (const sentence of sentences) {
      const pieces =
        sentence.length > maxChars
          ? splitLongSentence(sentence, maxChars)
          : [sentence];
      for (const piece of pieces) {
        if (current && current.length + 1 + piece.length > maxChars) {
          chunks.push(current);
          current = piece;
        } else {
          current = current ? current + " " + piece : piece;
        }
      }
    }
    if (current) chunks.push(current);
    return chunks;
  }

  /**
   * Split speakable text into ordered chunks of at most maxChars
   * characters, cutting at sentence boundaries. A single word longer
   * than maxChars occupies its own oversized chunk. Empty and
   * whitespace-only input yields an empty array.
   */
  function split(text, options) {
    const settings = options || {};
    const maxChars =
      Number.isFinite(settings.maxChars) && settings.maxChars > 0
        ? settings.maxChars
        : DEFAULT_MAX_CHARS;
    return packIntoChunks(segmentsOf(text, settings), maxChars);
  }

  /**
   * Split text into merged sentence segments without packing them into
   * chunks; exposed for diagnostics and tests.
   */
  function splitSentences(text, options) {
    return segmentsOf(text, options);
  }

  return {
    split: split,
    splitSentences: splitSentences,
    DEFAULT_MAX_CHARS: DEFAULT_MAX_CHARS,
  };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = SpeechChunker;
}
if (typeof window !== "undefined") {
  window.SpeechChunker = SpeechChunker;
}
