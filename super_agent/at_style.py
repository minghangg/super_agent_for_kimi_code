"""The questionnaire's stylesheet, in one place.

Separated from :mod:`at_page` because 186 lines of CSS in front of a template made
the template hard to find, and because a stylesheet edit deserves to be a one-file
diff. The tokens and the brace vocabulary are shared with ``_PAGE`` by convention,
not by import: ``at_page`` inserts this string as a **value**, so it needs no
escaping - which is exactly why it is a constant and not a function.
"""

_STYLE = """
:root {
  color-scheme: light dark;

  --ui-paper:        #f6f5f1;
  --ui-paper-2:      #eceae4;
  --ui-surface:      #ffffff;
  --ui-ink:          #1d2433;
  --ui-ink-strong:   #0b0f19;
  --ui-muted:        #4a5568;
  --ui-rule:         rgba(29, 36, 51, .12);
  --ui-accent:       #2f54eb;

  --ui-radius-card:  10px;
  --ui-radius-ctl:   6px;
  --ui-space-1:      4px;
  --ui-space-2:      8px;
  --ui-space-3:      12px;
  --ui-space-4:      16px;
  --ui-space-5:      20px;
  --ui-space-6:      24px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ui-paper:   #1a1a1a;
    --ui-paper-2: #222222;
    --ui-surface: #232323;
    --ui-ink:     #e8e6e3;
    --ui-ink-strong: #f4f2ef;
    --ui-muted:   #a9a6a1;
    --ui-rule:    rgba(232, 230, 227, .16);
    --ui-accent:  #7b93ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: var(--ui-space-6) var(--ui-space-5);
  background: var(--ui-paper);
  color: var(--ui-ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1rem;
  line-height: 1.5;
}
main {
  max-width: 44rem;
  margin: 0 auto;
}
.eyebrow {
  margin: 0 0 var(--ui-space-2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .6875rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--ui-muted);
}
h1 {
  margin: 0 0 var(--ui-space-4);
  font-family: Georgia, "Songti SC", serif;
  font-size: 2.25rem;
  font-weight: 600;
  line-height: 1.12;
  letter-spacing: -.015em;
  color: var(--ui-ink-strong);
}
.meta {
  margin: 0 0 var(--ui-space-5);
  font-size: .9375rem;
  color: var(--ui-muted);
}
.q {
  margin: 0 0 var(--ui-space-6);
  padding: var(--ui-space-5);
  background: var(--ui-surface);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-card);
}
.q p { margin: 0; }
.notice {
  margin: 0 0 var(--ui-space-5);
  padding: var(--ui-space-4) var(--ui-space-5);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-ctl);
  color: var(--ui-ink);
}
.notice[data-state="warn"] {
  border-color: var(--ui-ink-strong);
  color: var(--ui-ink-strong);
  font-weight: 600;
}
label {
  display: block;
  margin: 0 0 var(--ui-space-2);
  font-weight: 600;
  color: var(--ui-ink-strong);
}
textarea {
  display: block;
  width: 100%;
  padding: var(--ui-space-3);
  background: var(--ui-surface);
  color: var(--ui-ink);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-ctl);
  font: inherit;
  resize: vertical;
}
.hint {
  margin: var(--ui-space-2) 0 var(--ui-space-5);
  font-size: .875rem;
  color: var(--ui-muted);
}
fieldset {
  margin: 0 0 var(--ui-space-6);
  padding: 0;
  border: 0;
}
legend {
  margin-block-end: var(--ui-space-2);
  padding: 0;
  font-size: 1.125rem;
  font-weight: 700;
}
.options { display: grid; gap: var(--ui-space-3); }
.option {
  display: grid;
  grid-template-columns: 1.5rem minmax(0, 1fr);
  align-items: start;
  gap: var(--ui-space-3);
  min-height: 48px;
  padding: var(--ui-space-4);
  background: var(--ui-surface);
  border: 1px solid var(--ui-rule);
  border-radius: var(--ui-radius-card);
}
.option input {
  width: 1.25rem;
  height: 1.25rem;
  margin: .2rem 0 0;
  accent-color: var(--ui-accent);
}
.option label { display: block; cursor: pointer; }
.option-title { display: block; font-weight: 650; }
.option-description {
  display: block;
  margin-block-start: var(--ui-space-1);
  color: var(--ui-muted);
  font-size: .9375rem;
}
.option input:focus-visible + label {
  outline: 3px solid var(--ui-accent);
  outline-offset: 4px;
  border-radius: 2px;
}
.supplement { margin-block-end: var(--ui-space-5); }
textarea { min-height: 8rem; }
.submit-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--ui-space-4);
}
.submit-row .hint { margin: 0; flex: 1 1 16rem; }
button {
  min-height: 48px;
  padding: var(--ui-space-3) var(--ui-space-5);
  background: var(--ui-accent);
  color: var(--ui-surface);
  border: 1px solid var(--ui-accent);
  border-radius: var(--ui-radius-ctl);
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
button:hover { filter: brightness(.92); }
button:focus-visible,
textarea:focus-visible {
  outline: 3px solid var(--ui-accent);
  outline-offset: 3px;
}
@media (pointer: coarse) {
  .option, button { min-height: 52px; }
}
@media (forced-colors: active) {
  .option, button { border: 1px solid ButtonText; }
}
"""
