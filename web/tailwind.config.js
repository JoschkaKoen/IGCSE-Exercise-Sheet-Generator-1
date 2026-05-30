/**
 * Pre-compiled Tailwind config — replaces the runtime Tailwind Play CDN.
 *
 * Build the stylesheet with:  python scripts/build_tailwind.py
 * It scans the `content` globs for utility classes and emits
 * web/static/css/00-tailwind.css (committed; that file is what ships).
 *
 * ⚠️  RECOMPILE after adding/removing Tailwind utility classes in any template
 *     or JS file, then commit the regenerated 00-tailwind.css. Unlike the old
 *     Play CDN (which compiled in the browser), classes not present at build
 *     time will simply have no styles.
 */

// Subject cards are themed by a color name interpolated in Jinja
// (e.g. `text-{{ theme }}-400`, `bg-{{ theme }}-500/15`, `ring-{{ theme }}-400/60`).
// The scanner can't see the resolved color, so safelist the exact utility/shade/
// opacity shapes the templates use, across every family the theme could be (all
// standard families + the custom `commerce`). Opacity-modified classes must be
// explicit strings (regex patterns don't reliably cover them).
// KEEP IN SYNC if a template introduces a new interpolated shape — current set
// (web/templates/library.html + index.html):
//   text-{c}-300/400 · bg-{c}-500/10 · bg-{c}-500/15 · border-{c}-500/25
//   · ring-{c}-400/50 · ring-{c}-400/60   (the rest below are headroom)
const THEME_FAMILIES = [
  "slate", "gray", "zinc", "neutral", "stone", "red", "orange", "amber",
  "yellow", "lime", "green", "emerald", "teal", "cyan", "sky", "blue",
  "indigo", "violet", "purple", "fuchsia", "pink", "rose", "commerce",
];
const themeSafelist = [];
for (const c of THEME_FAMILIES) {
  themeSafelist.push(
    `text-${c}-200`, `text-${c}-300`, `text-${c}-400`, `text-${c}-500`,
    `bg-${c}-400`, `bg-${c}-500/10`, `bg-${c}-500/15`, `bg-${c}-500/20`,
    `border-${c}-500/25`, `border-${c}-500/40`,
    `ring-${c}-400/50`, `ring-${c}-400/60`, `ring-${c}-500/10`,
  );
}

module.exports = {
  // Paths are relative to this config file's directory (web/) — the standalone
  // CLI resolves `content` from the config location, and build_tailwind.py runs
  // it with cwd=web/ so both agree.
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        display: ["Sora", "system-ui", "sans-serif"],
        sans: ["Outfit", "system-ui", "sans-serif"],
      },
      colors: {
        space: { void: "#01040e", deep: "#080e1e", lift: "#112040", mist: "#334155" },
        // Business/Economics preview accent — a muted steel blue-gray.
        commerce: {
          200: "#c2d0e8", 300: "#9fb4d6", 400: "#7d97c2", 500: "#5e7aa6", 600: "#485f83",
        },
      },
    },
  },
  safelist: themeSafelist,
};
