# Board design notes

How the board looks and why. The values live in `coordinator/web/static/board.css`
(`:root`); the card is built in `coordinator/pages.py` (`_task_card`).

## Rules

- Light theme, system fonts, no CDN or framework.
- Never colour alone: every colour comes with a word or an icon.
- Red and amber mean "needs attention" and nothing else.
- Each fact appears once on a card. The column name is the state, so a card
  never repeats it.

## Tokens

| Kind | Columns | Edge / icon | Column | Text | ID chip |
|---|---|---|---|---|---|
| Waiting | Open, Ready | `#9B9A97` | `#F3F2F0` | `#37352F` | `#E3E2E0` |
| Active | In work | `#5B9FC9` | `#EAF3F8` | `#183347` | `#D3E5EF` |
| Handover | Needs clarification, To check | `#8E7CC3` | `#F3F0FA` | `#3E2A6B` | `#E3DCF3` |
| Finished | Done | `#6BA583` | `#EDF5ED` | `#1C3829` | `#DBEDDB` |
| Closed | Dropped | `#B5B2AD` | `#F5F4F2` | `#5A5651` | `#ECEAE7` |

Variables: `--k-<kind>-e`, `-bg`, `-t` and `-c`, where kind is `wait`, `act`,
`hand`, `fin` or `clo`. A class such as `.k-hand` sets `--ke`, `--kbg`, `--kt`
and `--kc` for everything inside it.

| Role | Background | Text | Edge |
|---|---|---|---|
| Amber: stale age, "needs you" | `#FFF4E2` | `#8A5300` | (none) |
| Red: blocked, urgent | `#FDECE8` | `#B3301A` | `#E0472B` |
| Link and live dot | | `#2A64D6` | |

"All good" (green notes, OK pill) uses the Finished colours.

## Task card

From top to bottom:

1. **Top row.** On the left, the ID chip: monospace, 12px, weight 600,
   padding 2px 8px, radius 6px, in the column's chip tint and text colour.
   On the right, the mode as an icon and a word.
2. **Title.** 14.5px (15px at 400px), weight 600, at most 2 lines. The full
   title is in the tooltip.
3. **Optional lines:**
   - live chat: a pulsing blue dot and "Chat f3a9 working · 25 min";
   - "Chat stopped" or "Lease ending" as a red pill;
   - "Blocked: reason" as a red pill;
   - on handover cards, the last note (2 lines at most), with a question
     icon for a question and a chat icon for a hand-off note.
4. **Footer.** The age on the left. A handover card that has waited 1 day or
   more shows its age as an amber chip with a clock icon. On waiting cards
   only, a "Start" button sits on the right; at 400px it fills the width at
   44px high, below the age.

Done and Dropped cards are quiet: the ID chip and the date on top, then the
title. They have no mode and no button.

The card is white, with radius 13px and padding 13px 14px 12px 18px. Its
shadow is `0 1px 1px rgba(9,30,66,.18)` with a 1px ring at
`rgba(9,30,66,.04)`. On hover it lifts 2px with a softer, larger shadow.

A 4px edge runs down the left side in the column's edge colour. Blocked or
urgent cards get a red edge (`#E0472B`) instead. Amber is never used for the
edge.

Column headers have a 22px solid round icon in the edge colour with a white
glyph, the column name, the kind, and a count. At 400px the columns become
accordions with the same header.
