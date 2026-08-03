# Design notes

Why the panels behave the way they do. Most of this exists because of one bug.

---

## The monitor that lied

Both panels shipped with this:

```js
catch (e) { render(DEMO); }
```

The intent was reasonable — the page should be meaningful before the collector exists. The effect was
not. When the collector died, the panel rendered a **healthy demo cluster**, under a clock that kept
ticking, on a wall where nobody was checking. It looked perfect. It was fiction.

It was found by accident while checking something unrelated. Everything below follows from it.

> **A monitor's failure mode must never look nominal.**

Three rules came out of it:

**Fall back to `LINK LOST`, never to data.** Red, unmistakable, and it says which thing is missing.

**A successful fetch is not proof of freshness.** A dead collector leaves its last `data.json` sitting
on disk, and the page will happily serve that forever. The payload carries an `updated` timestamp,
checked against a 30-second window — six poll intervals, so one missed tick never trips it.

**Demo data is opt-in only.** `?demo` in the URL, or keys `1`–`4` on the console panel to preview each
verdict state, `0` to go back to live. It can no longer happen by accident.

The same rule applies one level down. When the collector can't reach Uptime Kuma it emits an explicit
`services: null` — **not** an empty list — and the panel renders `KUMA LINK LOST` for that section. An
empty list would have drawn seven silent green rows.

---

## Every field is real

The reference material is full of wonderful decorative text: `CODE : 666`, `EXTENTION : 0256`,
`EX_MODE : DRIVE`, `PRIORITY : S+`. It would have been easy, and fun, to reproduce it verbatim.

The rule adopted instead: **nothing on screen is set dressing.** Only two things are frame rather than
data — the words `CURRENT` / `OPERATIONS`, and the unit names.

| Field | What it actually is |
|---|---|
| `CODE` | Running LXCs cluster-wide |
| `FILE` | Most recent finished cluster task, as `TYPE_STATUS` |
| `EXTENTION` | Guests defined cluster-wide (the misspelling is the reference's, and is kept) |
| `EX_MODE` | `QUORATE` / `DEGRADED` |
| `PRIORITY` | Worst failing tier, from the Kuma group names |
| Lobe fill | Blue opacity tracks that node's CPU; the busiest goes solid |
| Lobe alert line | A guest on that node whose rootfs is over `MAGI_GUEST_FS_WARN`; absent otherwise |

This has a cost, and paying it was the point. The most striking field in the whole reference —
**`TIME REMAINING TO COLLAPSE`**, counting down in the alert frame — was cut, because there is nothing
here to count down to. Binding it to the next backup window would have been the single most
Evangelion thing on the panel and the first lie on it.

---

## An alert is not a gauge

Guest filesystem pressure is the one thing on the panel that is usually *nothing*, and that shape
was chosen deliberately — it is the opposite of how the rest of the panel works.

The obvious implementation is a fourth bar beside MEM/DISK/SWAP tracking the fullest guest. It would
be honest and it would be useless: a bar sitting at 42% for eight months is one more number you stop
seeing, and the day it reads 84% it looks exactly like the day it read 42%. So the lobe's alert line
is set to the **empty string** when nothing is over the line. An SVG `<text>` with no content draws
nothing at all — no placeholder, no reserved space, nothing to habituate to. Its *appearing* is the
signal. It names the guest, because "something on this node is at 84%" is not actionable and
"kavita is at 84%" is.

Two things the placement forced, both found by looking at it rather than reasoning about it:

- **The text needs a black halo** (`paint-order: stroke fill`). The lobe body is not one colour — the
  busiest node renders solid blue, and red on solid blue collapses to a muddy maroon. Without the
  halo the alert is least legible on the node working hardest, which is precisely the one you most
  want to read.
- **It cannot reuse `blip`.** That animation bottoms out at `opacity:.25`, fine for a 6px dot whose
  only job is to move. This line carries a name and a number, and an alert you might glance at
  mid-cycle must never be unreadable. Its own `alertpulse` floors at `.55`.

It deliberately does **not** feed `crit`. A container filling its rootfs wants a human this week; the
EMERGENCY takeover asserts the cluster is broken *now*. Promoting one into the other spends the
takeover's credibility on a warning, which is how a red screen stops meaning anything.

### Why it comes from the Proxmox API and not from a monitoring service

Most clusters already have something watching disk thresholds, and reading the alert off it would be
less code. The numbers happen to be in the `/cluster/resources` call the collector already makes, so
sourcing them here costs nothing — but that is the cheap reason.

The real one: a monitoring service usually runs *as a guest on the cluster it monitors*. A panel
whose job is to say the cluster is in trouble must not depend on something inside the cluster to
tell it so. Same rule that says this panel is served from the box driving the screen rather than from
a node, and the same payoff — the failure renders instead of blanking.

If you do run a monitoring system with a disk threshold, set `MAGI_GUEST_FS_WARN` to match it. Two
tools disagreeing on screen about what counts as "too full" is worse than either alone.

---

## Fail-safe ordering

The verdict has four states and the order they're tested in is load-bearing:

```js
const crit = lost || !d.quorate || d.priority === "S+";
const warn = !crit && (inc > 0 || blind || online < expected);
```

Quorum is tested **first**, before anything is painted, so losing it can never render as `PROTECTED`
or even `DILEMMA`. And because a *missing* `quorate` field is falsy, unknown quorum is also critical —
it fails safe rather than nominal.

That nearly shipped a permanent alarm. The collector didn't emit `quorate` at all until it was caught
in a pre-deploy check; the panel would have gone to `EMERGENCY` the moment it saw real data and stayed
there. A field this important has to be *verified present*, not assumed.

**`LINK LOST` is deliberately not `EMERGENCY`.** `EMERGENCY` asserts the cluster is broken. `LINK LOST`
asserts only that we cannot see it. Both are red; only one blames the cluster. Units read 不明
*(unknown)* rather than 停止 *(stopped)*, because a dead collector is no evidence about any node.

### Quorum is a flag, not a count

`/cluster/status` reports `quorate` as a bare boolean. There are **no vote counts**, and a QDevice is
not listed at all. An earlier draft had the header showing `QUORUM 2/3` — those fields simply do not
exist. `pvecm status` has them, but reaching it would mean giving a wall display SSH into a cluster
node instead of a read-only API token. Not worth it for a header. It says `HELD` / `LOST`.

---

## Drama versus diagnosis

The full-screen red takeover is what catches your eye from across the room. It also shows only four
corner fields — so leaving it up for the whole incident means that at the moment things are worst, you
lose the schematic, the vitals and the per-unit detail.

That's backwards for an unattended console. Nobody sees the drama at 03:00; whoever walks up at 08:00
needs to know *what* broke.

So the critical state is split in two. `crit` lasts the whole episode and re-points every colour role
at red. `takeover` lasts **30 seconds** and then retires, leaving the full panel readable in the
critical palette.

It re-fires when the fault *changes* — keyed on its kind (link lost, quorum state, priority tier, which
unit is down) and never on the failing-check count. Keying on the count would let a number flickering
11→12→11 restart the window on every poll, and the takeover would never retire at all.

---

## Reading the source properly

The schematic geometry was traced out of actual frames rather than eyeballed. The finding that
mattered: the connecting bands branch from the **exact midpoint** of each chamfer, and the chamfer
carries on past the branch — measured at (527, 400) against a branch at (525, 397), and the same on the
other side. Corner-to-corner joins, which an earlier version used, give a subtly wrong silhouette: the
junctions read as folds rather than as a branching bus.

On this canvas that puts the midpoints at (430,300), (650,300), (320,410) and (760,410), with both
bands at a true 45° and the whole figure mirrored about x=540.

The four shapes **tile** — every boundary between two of them is one shared edge, stroked by both,
which is what renders as a single solid band. Drawing the central keystone alone produces both
diagonal bands *and* every shared edge, because its remaining sides lie exactly on the units' chamfers.

### Three colours, and they never cross over

| Colour | Role |
|---|---|
| Orange | Frame, structure, connectors, all label text |
| Blue | The MAGI unit bodies, and nothing else |
| Green | Accent rules and nominal status |
| Red | **Reserved** — appears only when something is genuinely wrong |

Because red is reserved, a red screen is never decorative.

One trap that follows: **canvas and inline styles cannot read a CSS variable.** Every colour written
from JavaScript — the sparkline, the load-scaled unit fill, the canvas grid — is declared as a variable
and read back per render. Miss that and the critical palette leaves cheerful blue traces sitting on a
red screen, which is exactly what happened for half a day.

---

## Type

Evangelion's own faces, per [Fonts In Use](https://fontsinuse.com/uses/28760/neon-genesis-evangelion):
**Matisse EB** on the NERV interfaces and HUD displays — an ultra-bold Mincho that Anno chose against
the era's fashion for sans — with **Eurostile** on signage and UI elements.

Both are commercial. The panels use free stand-ins:

| Role | Stand-in |
|---|---|
| Matisse EB → Japanese | Noto Serif CJK JP Black (`fonts-noto-cjk-extra`) |
| Eurostile → Latin caps | Michroma (OFL, bundled) |

The CJK entry sits **last** in every font stack on purpose. Per-character fallback means Latin resolves
earlier and only kana/kanji reach the Mincho, so `稼 働 · B` gets a Mincho 稼働 and a Latin B.
`Eurostile` is named **first**, so a licensed copy takes over with no code change.

Michroma has **one weight**, so everything set in it stays at `font-weight: 400` — synthetic bold
smears the verticals on a face this wide. It also runs about 35% wider than a condensed fallback, so
every size using it was cut and its tracking reduced to near zero.

---

## Things deliberately not done

**No temperature.** The Proxmox API exposes no sensor data whatsoever, so a temperature readout means
adding a second data source and its own auth lifecycle for a single number. The third gauge is swap
pressure, which is arguably the better vital anyway: it's what tells you a node is in trouble before it
starts thrashing.

**Kuma is rendered, not embedded.** Its status page has a Custom CSS box and could have been themed in
place, or iframed. But an iframe keeps its own scaling and chrome, and a CSS theme is selectors written
against a DOM that is not an API — a Kuma upgrade could silently break the wall display. Owning the
markup means it can't. The same fetch also supplies `PRIORITY`, `CHECKS` and the incident count.

**No webfont fetch at runtime.** The page must render correctly on a cold boot with no network. Fonts
are installed as system fonts, not `@font-face`d from the local server — a webfont fetch is exactly the
kind of silent degradation a wall display hides well.

**No build step.** Each panel is a single self-contained HTML file with its CSS and JS inline. You can
read the whole thing top to bottom, and deploying it is `scp`.
