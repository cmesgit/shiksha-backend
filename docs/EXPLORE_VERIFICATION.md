# Explore community verification — design handoff

Transcribed **2026-11-12** from the Claude Design project
*"Student teacher dashboard redesign"* (project id `a733c6f3-a7fd-4441-96be-195002c05714`),
pages `Explore.dc.html`, `Verification.dc.html`, `Verification Admin.dc.html`,
`Document.dc.html`, `My Library.dc.html`.

> ⚠ **Why this file exists.** The artboard only renders while a claude.ai
> session is live, the project is scheduled to move on 2026-12-14, and the
> `.dc.html` sources are **not extractable** — the artboard is a cross-origin
> sandbox iframe (`*.claudeusercontent.com/_bootstrap`) that `get_page_text`
> and `read_page` cannot pierce, it serves nothing when opened by URL, and
> clipboard reads are denied. **This markdown is the spec.** If you need a
> detail that is not written here, re-open the design project and add it here
> rather than relying on the link.

Unlike `design_handoff_content_studio`, there is **no `src/` scaffold** — the
design pages are a single inline-styled HTML file each, with their own
localStorage sample data. Build from this spec, not from that HTML.

---

## 1. What this is

Today an Explore upload goes live the moment it is submitted, and nothing on
the platform ever marks it as *good* — there is moderation (report → remove)
but no positive signal. The design adds **community verification**:

> Every upload goes live as **Unverified**. It becomes **Verified** when **3
> members who specialise in one of its tags** confirm it is accurate.

This is distinct from the **Research Hub** (`documents/research_views.py`),
which is a formal, admin-brokered peer review of research papers. Research
papers continue to go through Research Hub and are explicitly routed away from
this flow.

### Scope confirmed by the owner (2026-11-12)

| Question | Answer |
| --- | --- |
| First slice | **Verification system end-to-end** — models, API, reviewer page, admin page, status on Explore cards + Document page |
| Upload limits | All designed file types, but **50 MB, not the design's 200** — widened to 200 on 2026-11-12, reverted by the owner 2026-11-13 (see §7) |
| Taxonomy | **Follow the design** — field › subject › branch (see §6) |
| Multi-file sets | **Build them for real** — a set groups several files; verification attaches to the set (see §8) |
| My Library | **Replace with the design's page** (see §8) |

The last two were put to the owner because each loses something: multi-file
sets need a data migration over every existing Explore document, and the
design's My Library drops four tabs the live page has. Both were chosen
knowingly, in favour of design fidelity.

---

## 2. Vocabulary

The design renames things. Map them before writing code:

| Design word | Today's code | Note |
| --- | --- | --- |
| **set** | `documents.Document` | The design's unit is a *set of files*; today a Document is **one** file. See §8. |
| **tag** | `Document.subject` + `Document.topic` | A tag in the design is a full `Field › Subject › Branch` path, and a set has **several**. |
| **field** | — | **No equivalent today.** New top level. |
| **subject** | `Document.subject` | Free text today. |
| **branch** | `Document.topic` | Free text today. Labelled "Subcategory" on the live Document page. |
| **document type** | `documents.DocumentCategory` | Unchanged — the 8 seeded categories. |
| **specialist / reviewer** | — | New. Not `ResearchReviewer`, which is research-paper-only. |
| **moderator** | `documents.moderate` permission | The existing Explore moderator panel is unrelated to verification. |

---

## 3. States

A set is in exactly one verification state.

| State | Badge | Meaning |
| --- | --- | --- |
| `UNVERIFIED` | amber "Unverified" + `N of 3 confirmations` | Live and public, awaiting confirmations. The state every upload starts in. |
| `VERIFIED` | green "Verified" + `Confirmed by 3 specialists` | Reached the confirmation threshold. |
| `CHANGES_REQUESTED` | red "Changes requested" + `Confirmations paused at N of 3` | A reviewer answered **No** to a check. **Stays in Browse, still marked Unverified to the public**, confirmations frozen. |
| `HIDDEN` | admin-only | Two change requests on the same version. Leaves Browse and appears under admin → Needs attention. |

### Transition rules (verbatim from the "How it works" tab)

- Only confirmed specialists' reviews count, and only in their own subjects.
- Nobody can review their own upload. Each member reviews a version once.
- A set with changes requested stays in Browse, marked Unverified, with its
  confirmations paused.
- Uploading a new version clears earlier confirmations and restarts the count.
- Two specialists requesting changes on the same version hides the set until a
  moderator has looked at it.
- Editing the files of a Verified set returns it to Unverified.

---

## 4. The reviewer page — `Verification.dc.html`

Route in the design: `Verify`, fourth item in the Explore toolbar
(`Research · Home · Browse · Verify`). Proposed real route: `/explore/verify`.

Header: eyebrow `COMMUNITY VERIFICATION`, h1 **"Check uploads in your
subjects"**, sub: *"Every upload goes live as Unverified. It becomes Verified
when 3 members who specialise in one of its tags confirm it."* (the "3" is the
live setting value, not a literal).

Five tabs. Counts in the labels are live.

### 4a. Review queue · N

Caption: `N sets waiting in <subjects>, longest wait first`.

Each row: document-type eyebrow in caps → title → `<uploader> · <N> files ·
waiting <duration>` → status pill + meta line → **Review** button (right).

Meta line composes from whatever applies, dot-separated:
`1 of 3 confirmations · Invited · 1 day left to respond · reminder sent`

Ordering is **longest wait first**.

### 4b. Review detail

Reached from **Review**. `‹ Back to queue` above a two-column layout.

**Left — the set.** Type eyebrow, title, `<uploader> · uploaded <N> days ago ·
N of 3 confirmations`, tag chips, then `Files to check` — each file a row of
name + size with an **Open** button.

**Right — `YOUR REVIEW`**, sub `Reviewing as a <Subject> specialist`. Three
checks, each a Yes/No pair:

1. `The files match the title and tags`
2. `The content is accurate`
3. `The files open and are complete`

Then `Note for the uploader` and the submit button. Both are **conditional**:

| All three Yes | Any No |
| --- | --- |
| Note label `Note for the uploader (optional)` | Note label `Note for the uploader (required)` |
| Button **Confirm as accurate** | Button **Request changes** |

Button is disabled until all three are answered (and, when any No, until the
note is non-empty). Footnote under the button: *"Open every file before you
send your review."*

Selected Yes = solid green pill; selected No = red-tinted outline pill.

### 4c. Notifications · N

`N unread of M` on the left, **Mark all as read** on the right. Each row: unread
dot, bold title line, grey sub-line, action button on the right. Four kinds:

| Title | Sub | Button |
| --- | --- | --- |
| `Reminder: 1 day left to review "<title>"` | `<Subject> · <uploader>` | Review |
| `You are invited to review "<title>"` | `<Subject> · <uploader>` | Review |
| `"<title>" is now Verified` | `Confirmed by 3 specialists` | View |
| `Changes requested on "<title>"` | the reviewer's note | See note |

### 4d. My uploads

The uploader's side. One card per own set: status pill + progress text on the
first line, title, tag path. A `CHANGES_REQUESTED` card additionally shows a
red `Reviewer's note` panel and an **Upload a new version** button.

### 4e. Specializations

Intro: *"Your reviews count in subjects where your specialization is confirmed.
Teacher accounts are confirmed in the subjects they teach. Anyone else is
confirmed once 2 of their own uploads in the subject are verified, or when an
admin approves their request."*

One row per subject: subject name, a grey reason line, a **Confirmed** pill and
an `×` to remove. Reason lines seen: `Teacher account`, `2 of your uploads in
this subject are verified`, (and by implication `Approved by an admin`).

Below, `Add a subject` with `+ <Subject>` chips. **Adding a subject creates a
request on the admin page's Reviewers tab** — it does not grant anything.

### 4f. How it works

Four cards — three numbered steps plus one exception card:

1. **Upload** — "The set goes live straight away and is marked Unverified."
2. **Specialists review** — "It enters the queue of members confirmed in any
   subject it is tagged with. Each answers three checks."
3. **Verified** — "After 3 members answer yes to all three checks, the set is
   marked Verified."
- *If something is wrong* → **Changes requested** — "A no on any check sends the
  set back to the uploader with the reviewer's note."

Then a **Rules** list — the six bullets in §3.

---

## 5. The admin page — `Verification Admin.dc.html`

Reached from the reviewer page as "Admin settings"; also the `Moderator` button
in the Explore toolbar. Title **"Settings and reviewers"**, eyebrow
`VERIFICATION`. Four tabs: `Settings`, `Reviewers · N requests`,
`Needs attention · N`, `Research papers · N`.

> The **Research papers** tab is the existing admin Research Hub
> (`Admin-dashboard/src/pages/ResearchHub.jsx`) re-presented as a tab. It is
> **already built** and out of scope for this work.

### 5a. Settings

At the top, a live-recomputed summary box — `WHAT HAPPENS TO AN UPLOAD WITH
THESE SETTINGS` — restating the current configuration as six numbered
sentences. It must be derived from the settings, not hardcoded:

1. It is published straight away with an Unverified badge.
2. The 3 least busy reviewers in its subjects are invited. Each has 7 days
   before the invite passes on.
3. Never invited: the uploader, anyone from the same institution, reviewers
   with 5 open reviews.
4. If nobody is available, reviewers from the same field are invited, then it
   comes to you.
5. After 3 confirmations it is marked Verified.
6. A change request sends it back to the uploader. After 2 on one version it is
   hidden and comes to you.

Then four groups of settings. **These are the exact labels, help text, control
type and default.**

#### Reaching Verified

| Setting | Help | Control | Default |
| --- | --- | --- | --- |
| Confirmations needed | How many reviewers must confirm a set before it is marked Verified. | stepper | **3** |
| Show unverified uploads in Browse | On: uploads are public at once with an Unverified badge. Off: they stay private until Verified. | toggle | **On** |
| Change requests before a set is hidden | After this many on one version, the set leaves Browse and comes to you. | stepper | **2** |

#### Assigning reviewers

| Setting | Help | Control | Default |
| --- | --- | --- | --- |
| How reviewers are chosen | The least busy reviewers in the subject are invited by notification. The set also stays in the open queue. | segmented: `Open queue` / `Invite automatically` / `Admin assigns` | **Invite automatically** |
| Reviewers invited per set | Invited in order of fewest open reviews. | stepper | **3** |
| Time to respond | An unanswered invite passes to the next reviewer after this. | stepper (days) | **7 days** |
| Send to admins when no reviewer exists | Sets tagged with a subject nobody reviews appear under Needs attention. | toggle | **On** |

The help text under *How reviewers are chosen* changes with the choice.
Behavioural differences:
- **Open queue** — no invites, no deadlines; the queue is every eligible set.
- **Invite automatically** — invites *and* the set stays in the open queue.
- **Admin assigns** — the queue holds only sets assigned to that reviewer.

#### Automatic matching

| Setting | Help | Control | Default |
| --- | --- | --- | --- |
| Widen to the same field | When a subject has no reviewers, reviewers from other subjects in the same field are invited before the set comes to you. | toggle | **On** |
| Prefer the same branch | Reviewers whose own verified uploads are in the set's branch are invited first. | toggle | **On** |
| Skip reviewers from the uploader's institution | The uploader is never asked to review their own set. This also leaves out their colleagues and classmates. | toggle | **On** |
| Open reviews per reviewer | A reviewer at this limit is not invited again until they finish one. | stepper | **5** |
| Remind invited reviewers | A notification is sent before the invite runs out. | toggle | **On** |
| Reminder sent | Before the time to respond ends. | stepper (days) | **2 days** |
| Pause inactive reviewers | A reviewer who keeps missing invites stops receiving them. You can resume them on the Reviewers tab. | toggle | **On** |
| Missed invites in a row | Before the reviewer is paused. | stepper | **3** |

#### Who can review

| Setting | Help | Control | Default |
| --- | --- | --- | --- |
| Teacher accounts | Teachers review in the subjects they teach, with no extra step. | toggle | **On** |
| Members who earn it | A member becomes a reviewer in a subject through their own verified uploads. | toggle | **On** |
| Verified uploads needed | In the same subject. | stepper | **2** |
| Members who request it | A member sends proof of their background and an admin approves or declines. | toggle | **On** |

Footer: **Save settings** (disabled when clean) + status text `All changes
saved.` + **Reset demo data** (design-only — do **not** build this).

### 5b. Reviewers · N requests

**Requests to review** — one card per pending request: member name, bold green
`Wants to review <Subject>`, a grey evidence line (e.g. *"M.Tech Structural
Engineering, NIT Silchar. Degree certificate attached."*), and **Approve** /
**Decline**.

**Reviewers** — a table of current reviewers:
`<name>` / `<subjects, comma-joined> · <how they qualified> · <N> reviews · <N>
open` / status pill / action.

| Status pill | Action button | Qualification strings seen |
| --- | --- | --- |
| `Active` (green) | Pause | `Teacher account` |
| `At limit` (amber) | Pause | `Earned from verified uploads` |
| `Paused` (amber) | Resume | `Approved request` |

`At limit` is derived — open reviews == *Open reviews per reviewer*. It is not a
stored state.

### 5c. Needs attention · N

Leading copy, both lines required:

> Most sets never appear here. This list only holds the ones the automatic flow
> could not finish.
>
> **N sets are being handled automatically under your current settings.**

Two kinds of card:

| Pill | Shows | Actions |
| --- | --- | --- |
| `Invites expired` (amber) | `N of 3 confirmations`, title, tag path | **Assign a reviewer** |
| `Hidden after 2 change requests` (red) | `N of 3 confirmations`, title, tag path, then **every** change-request note as a red panel titled `<Reviewer> requested changes` | **Restore to Browse** / **Remove set** |

---

## 6. Taxonomy — field › subject › branch

The browse panel on Explore is three stacked rows inside one card, each row a
label + chips with counts:

```
FIELD                     Engineering 8 | Social Sciences 4 | Sciences 1 |
                          Humanities 0 | Commerce 0 | Exam Prep 2
SUBJECT IN ENGINEERING    Civil Engineering 7 | Mechanical Engineering 0 |
                          Computer Science 1 | Electrical Engineering 0
BRANCH OF CIVIL ENG.      Structural 1 | Geotechnical 1 | Transportation 2 |
                          Water resources 1 | Surveying 1 |
                          Construction management 0 | Environmental 0
```

Selected chip = solid green. Counts show for every chip including zero.
Heading below: `Engineering › Civil Engineering` + `N file sets`, a `Sort`
select (`Most downloaded`), then two filter chip rows (§9).

**Fields seen in the design:** Engineering, Social Sciences, Sciences,
Humanities, Commerce, Exam Prep.

A tag is the whole path. A set carries **several** paths — the sample set
*"Displacement and resettlement in dam projects"* is tagged both
`Engineering › Civil Engineering › Water resources` **and**
`Social Sciences › Sociology › Rural sociology`, and **either** tag makes a
specialist eligible to review it.

Members may **create a new branch** during upload if what they type isn't in
the list, with **no approval** — this matches today's `taxonomy.py:subject_groups()`
behaviour, which unions a static tree with the pairs actually present on
visible documents.

> **Implementation note.** Today `Document.subject` / `Document.topic` are two
> free-text CharFields holding at most one pair. The design needs (a) a third
> level above subject and (b) several paths per set. Neither fits the current
> columns. See BUILD_GUIDE Phase 6.

---

## 7. Upload — `Explore.dc.html`, screen `upload`

Title **"Upload to the library"**. Sub: *"Share notes, slides or question
papers with other learners. Add one or more files as a set, then tag every topic
they belong to. Research papers go through **Research Hub** for review."*

Three numbered sections.

### 1 · FILES
Dropzone: **"Drop files here"** / *"Documents, slides, spreadsheets and data,
images, drawings, code, audio, video and ZIP archives. Up to 50 MB per file."*
/ **Choose files**.

> ⚠ The design says **200 MB**. Built at 200, then **reverted to 50 by the
> owner on 2026-11-13** — the file TYPES stay widened, only the size came back
> down. This is the one place the implementation deliberately departs from the
> design, and the reason is storage rather than validation: see the Bunny note
> below. Below it `N files` and a removable row per file
(type glyph, name, size, `×`).

> **This is the all-types change the owner approved.** Before it, the limit
> was **7 extensions at 50 MB**, enforced server-side at
> `shiksha-backend/config/upload_validation.py:30` (`DOCUMENT_EXTS`) and
> `validate_upload()`, which also does a **magic-byte signature sniff** — every
> newly allowed type needs a signature entry or it will be rejected.
>
> 🔴 **Infra consequence, and the reason the size was reverted:** dev and prod
> share **one public Bunny storage zone**, a pull zone serves its whole storage
> zone **unauthenticated**, and media has **no backup**. A 200 MB ceiling on
> user-uploaded video was the part of this change that could not be undone
> after the fact, so it is back at 50. The wider type list still puts new
> kinds of content on that zone — raise the storage question before deploying.
> See the `bunny-cdn-serves-private-docs-unauthed-2026-09-09` memory.

### 2 · ABOUT THIS SET
`Title` (required) · `Description (optional)` placeholder *"Course, semester,
what the files cover"* · `Document type` as chips — Books, Articles, Notes,
Study Materials, Presentations, Assignments, Question Papers, and a final chip
**`Research paper › Research Hub`** which navigates away · `Institution`
placeholder *"Your college / school"* · `Language` select.

### 3 · TAGS
Help: *"One set of tags covers every kind of file. Search any subject or branch
and add as many as apply, or create a new one. Tags also decide who can verify
the set."*

Selected tags are solid green chips showing the full path with the **branch in
bold** and an `×`. Below, a search input placeholder *"Type a subject or branch,
e.g. Hydrology"*, and a suggestion row `Other branches of <Subject>` with
`+ <Branch>` chips.

> The design **removed** the old comma-separated free-text `Tags` field. The
> field/subject/branch path *is* the tag system now.

### Ready to publish
Side panel: `READY TO PUBLISH`, title, `N files`, each tag path on its own line,
a full-width **Publish** button, footnote *"Goes live straight away, marked
Unverified."*, then a rule and *"**How verification works.** The set becomes
Verified once 3 members who specialise in one of its tags confirm it is
accurate."*

Publish stays disabled until there is ≥1 file, a title and ≥1 tag. While
publishing the button reads **"Publishing…"** and is disabled; on failure a
message appears in the panel and **the form keeps everything entered**.

Signed-out visitors see **"Sign in to upload"** with a Log in button instead of
the form, and `My Library` / `Moderator` are hidden from the toolbar.

---

## 8. Explore library — verification surfaces

### Card (the main grid)
Type eyebrow → title → `<author> · <size> · <N> downloads` → **status pill +
progress text** → `N files: DOCX, XLSX` (only when > 1 file) → tag chips → and,
when the viewer specialises in one of the tags, a tinted call-to-action panel:

> You specialise in **<Subject>**. Have you checked these files?
> **[ Review this set ]**

Progress text is `Confirmed by 3 specialists` when verified, `N of 3
confirmations` otherwise.

### Recommended / Recently uploaded rails
Compact cards carrying **only** the status pill (no progress text).

### Document page — `Document.dc.html`
Title block carries `<author> · <size> · <N> downloads` and the status pill +
progress. Actions **Download all** / **Save to My Library**.

Two columns:
- **Left** — `FILES` (one row per file: glyph, full filename, **Preview**) and
  `TAGS` (one chip per full path).
- **Right** — `VERIFICATION` panel: a heading (`Partly confirmed`), a sentence
  *"2 more confirmations needed from specialists in Civil Engineering or
  Sociology."*, a row per confirming specialist (green check + name), and a
  link **How verification works ›**. Below it a `HISTORY` panel — one dated row
  per event (`Version 1 uploaded by <name>`, `Confirmed by <name>`).

> `Download all` is **dead in the design** — the project notes list it under
> known gaps. Build it or drop the button; do not ship it inert.

### My Library — `My Library.dc.html`
Title **"My Library"**, sub *"Sets you save from Browse are kept here."* A
single list of saved sets. Empty state: **"Nothing saved yet"** / *"Open a set
in Browse and choose Save to My Library."* / **Browse the library**. Below the
list, always: *"Your own uploads and their verification status are under
**Verify**, on the My uploads tab."*

⚠ **This replaces a richer page.** The live `/explore/dashboard` has six tabs
(Overview, Reading History, My Uploads, Saved, Following, Collections). The
owner chose the design's version on 2026-11-12. **Remove the tabs from the
screen, not the data or the routes** — `/explore/collections`,
`/explore/collections/:id` and `/explore/author/:id` stay reachable, the
localStorage store keeps recording history and follows, and nothing is
deleted. Only the dashboard UI changes, so the decision stays reversible.

---

## 9. Filters

Two chip rows above the results grid, both single-select, first chip default:

- **Status** — `Any status` · `Verified` · `Unverified` · `Needs my review · N`
- **File type** — `All files` · `Documents` · `Slides` · `Data` · `Images` ·
  `Audio and video` · `Other`

`Needs my review` is the viewer's queue — unverified sets tagged with a subject
they are confirmed in, excluding their own and ones they already reviewed.

---

## 10. Visual tokens

From the design project's own `CLAUDE.md`:

```
#0B5B3E  primary          #0F9D6B  accent green
#EAF7F0  tint             #F7FCF9  page wash
#E4EEE8  lines            #0C2620  ink            #5A7268  muted
#FFF3D6 / #8A5A00  amber (Unverified, At limit, Paused)
#FDECE7 / #8A2B10  red   (Changes requested, Hidden)
Headings: Plus Jakarta Sans    Body: Inter
Hit targets 44px. Layouts fluid — flex-wrap, auto-fit grids, clamp padding.
Copy: plain, short, no jargon.
```

> ⚠ These are the **design project's** palette, not the app's. The public site
> (`shiksha-frontend`) has its own tokens — resolve every colour through those
> and add tokens where the greens/ambers/reds above have no equivalent. Do not
> paste these hexes into components.

---

## 11. Known gaps in the design

Recorded so nobody mistakes them for requirements:

- `Courses` and `About` in the Explore header link nowhere.
- `Download all` on the Document page does nothing.
- The `Open` / `Preview` buttons on files do nothing.
- The admin **Needs attention** list and **Reviewers** table are sample data —
  assigning a reviewer there does not reach anyone's queue.
- No signed-out view of the Verify page.
- Phone layouts were never checked after the toolbar, category grid and
  verification rows were added.
- Uploaders are not blocked from confirming their own sets in the prototype,
  even though the rules say they must be.
