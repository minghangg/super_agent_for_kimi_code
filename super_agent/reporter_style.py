"""The Reporter stylesheet (ISS-132 F).

Moved out of ``reporter.py`` unchanged. Why a module of its own: this is 160
lines of CSS literal with no caller but ``render``, and it is data, not logic -
keeping it inline pushed the renderer past 300 lines for no reason. The page it
styles is untouched; ``reporter.STYLE`` is the same string, imported here.

No function in this file: it exists so that a stylesheet edit is a one-file
diff and so that ``reporter.py`` reads as the collector plus the renderer.
"""

#: One stylesheet, three token tiers, five cascade layers. Literal values exist
#: only in tier 1 and in the reset; semantic tokens build on them and component
#: tokens build on semantic ones. No universal selector is used (a ``*`` reset
#: cannot be overridden by a lower layer), and every geometry property is
#: logical so the page does not silently change meaning under a vertical script.
STYLE = """
@layer reset, base, theme, components, utilities;

@layer reset {
  html, body, h1, h2, h3, p, ul, ol, dl, dd, figure { margin: 0; }
  html, body, header, main, footer, nav, section, li, dl, div, details {
    box-sizing: border-box;
  }
  ul, ol { padding-inline-start: 0; list-style: none; }
  dd { margin-inline-start: 0; }
  body { min-block-size: 100vh; min-block-size: 100dvh; }
  a { color: inherit; }
}

@layer theme {
  :root {
    /* tier 1 — literals */
    --hue: 205;
    --hue-ok: 152;
    --hue-warn: 35;
    --font-sans: system-ui, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --size-1: 0.75rem;
    --size-2: 0.875rem;
    --size-3: 1rem;
    --size-4: 1.375rem;
    --size-5: 1.875rem;
    --space-1: 0.25rem;
    --space-2: 0.5rem;
    --space-3: 0.75rem;
    --space-4: 1rem;
    --space-5: 1.5rem;
    --space-6: 2rem;
    --radius-1: 8px;
    --radius-2: 14px;
    --measure-1: 60rem;
    --neutral-0: hsl(var(--hue) 40% 100%);

    /* tier 2 — semantic */
    --color-page: hsl(var(--hue) 32% 97%);
    --color-surface: hsl(var(--hue) 40% 99.5%);
    --color-text: hsl(var(--hue) 22% 18%);
    --color-muted: hsl(var(--hue) 12% 42%);
    --color-line: hsl(var(--hue) 20% 86%);
    --color-accent: hsl(var(--hue) 42% 38%);
    --color-on-accent: var(--neutral-0);
    --color-ok: hsl(var(--hue-ok) 42% 28%);
    --color-ok-bg: hsl(var(--hue-ok) 42% 93%);
    --color-warn: hsl(var(--hue-warn) 72% 30%);
    --color-warn-bg: hsl(var(--hue-warn) 82% 93%);
    --color-unknown: hsl(var(--hue) 10% 34%);
    --color-unknown-bg: hsl(var(--hue) 12% 92%);
    --font-body: var(--font-sans);
    --font-code: var(--font-mono);
    --measure-page: var(--measure-1);
    --space-gutter: var(--space-4);

    /* tier 3 — component */
    --card-pad: var(--space-4);
    --card-gap: var(--space-3);
    --card-radius: var(--radius-2);
    --card-surface: var(--color-surface);
    --card-line: var(--color-line);
    --shell-gap: var(--space-5);
    --chip-pad-inline: var(--space-2);
    --chip-pad-block: var(--space-1);
    --chip-radius: var(--radius-1);
    --chip-font: var(--size-1);
    --badge-pad-inline: var(--space-3);
    --badge-pad-block: var(--space-2);
    --badge-radius: var(--radius-1);
    --badge-font: var(--size-3);
  }
}

@layer base {
  body {
    font-family: var(--font-body);
    font-size: var(--size-3);
    line-height: 1.6;
    color: var(--color-text);
    background: var(--color-page);
  }
  code { font-family: var(--font-code); font-size: var(--size-2); }
  summary { cursor: pointer; font-size: var(--size-4); font-weight: 650; }
  h2 { font-size: var(--size-4); letter-spacing: -0.01em; }
  h3 { font-size: var(--size-2); color: var(--color-muted); text-transform: uppercase; }
  :focus-visible { outline: 3px solid var(--color-accent); outline-offset: 3px; }
}

@layer components {
  .shell {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--shell-gap);
    max-inline-size: var(--measure-page);
    margin-inline: auto;
    padding-block: var(--space-6);
    padding-inline: var(--space-gutter);
  }
  .page-header { display: flex; flex-wrap: wrap; gap: var(--card-gap);
    align-items: baseline; }
  .page-header h1 { font-size: var(--size-5); letter-spacing: -0.02em;
    flex: 1 1 18rem; min-inline-size: 0; }
  .meta { color: var(--color-muted); font-size: var(--size-2); }
  .toc ul { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .toc a { color: var(--color-accent); text-underline-offset: 0.2em; }
  .card { background: var(--card-surface); border: 1px solid var(--card-line);
    border-radius: var(--card-radius); padding: var(--card-pad);
    display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--card-gap); }
  .badge { display: inline-flex; flex-wrap: wrap; gap: var(--space-2);
    align-items: center; padding-inline: var(--badge-pad-inline);
    padding-block: var(--badge-pad-block); border-radius: var(--badge-radius);
    font-size: var(--badge-font); font-weight: 650; }
  .badge--none { background: var(--color-ok-bg); color: var(--color-ok); }
  .badge--action { background: var(--color-warn-bg); color: var(--color-warn); }
  .badge--unknown { background: var(--color-unknown-bg);
    color: var(--color-unknown); }
  .shell-stack { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--shell-gap); }
  .items { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--card-gap); }
  .item { border-inline-start: 3px solid var(--card-line);
    padding-inline-start: var(--space-3); display: grid;
    grid-template-columns: minmax(0, 1fr); gap: var(--space-2);
    min-inline-size: 0; }
  .item-head { display: flex; flex-wrap: wrap; gap: var(--space-2);
    align-items: baseline; }
  .chip { background: var(--color-page); border-radius: var(--chip-radius);
    padding-inline: var(--chip-pad-inline);
    padding-block: var(--chip-pad-block); font-size: var(--chip-font);
    color: var(--color-muted); }
  .locator { font-family: var(--font-code); font-size: var(--size-2);
    min-inline-size: 0; overflow-wrap: anywhere; }
  .stamp { font-size: var(--size-1); color: var(--color-muted); }
  .expiry { font-size: var(--size-1); border-radius: var(--chip-radius);
    padding-inline: var(--chip-pad-inline);
    padding-block: var(--chip-pad-block); }
  .expiry--fresh { background: var(--color-ok-bg); color: var(--color-ok); }
  .expiry--stale { background: var(--color-warn-bg); color: var(--color-warn); }
  .expiry--unknown { background: var(--color-unknown-bg);
    color: var(--color-unknown); }
  .facts { display: grid; grid-template-columns: minmax(6rem, max-content)
    minmax(0, 1fr); gap: var(--space-1) var(--space-3); font-size: var(--size-2); }
  .facts dt { color: var(--color-muted); }
  .facts dd { min-inline-size: 0; overflow-wrap: anywhere; }
  .plain { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--space-1); font-size: var(--size-2); }
  .merged { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--space-1); font-size: var(--size-2);
    padding-inline-start: var(--space-4); }
  .warn { color: var(--color-warn); font-size: var(--size-2); }
  .details { border-style: dashed; }
}

@layer utilities {
  .muted { color: var(--color-muted); }
}
"""
