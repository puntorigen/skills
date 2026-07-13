---
name: nextjs-onboarding-tours
description: Add a layered onboarding experience to any Next.js app or admin panel - a first-login welcome wizard, re-launchable guided tours per area (driver.js), task-based dashboard quick actions, an in-app help center, and a floating help button, with per-user persistence. Use when the user asks to add onboarding, a welcome wizard, guided tours, product tours, an admin tour, walkthroughs, spotlight tutorials, or in-app help to a Next.js application or CMS admin (Payload, custom dashboards, SaaS apps).
---

# Next.js Layered Onboarding & Guided Tours

Builds a complete onboarding layer for non-technical users of a Next.js app. The design principle: **task-oriented, never nagging**. Users think "I want to publish a news article", not "go to the Posts collection". Every piece auto-shows at most once, and everything stays re-launchable from a help center.

## The five layers

1. **Welcome wizard** — branded full-screen modal on first login. ~4 steps: welcome → map of the navigation → key concept (e.g. how the AI copilot works) → "pick your first task" cards. Always shows an "Explore on my own" escape hatch.
2. **Guided tours** — driver.js spotlight tours per area (dashboard, editor, inbox…). Auto-start on first visit to each area; re-launchable forever.
3. **Quick actions** — a "What do you want to do today?" card row on the dashboard with 5–6 task deep-links.
4. **Help center** — a static route/view with tour relaunch cards, collapsible step-by-step guides (typed TS data, no CMS), and an escalation contact box.
5. **Floating help button** — bottom-right "?" FAB on every screen: start this screen's tour / open help center / replay the wizard.

## Workflow

Copy this checklist and track progress:

```
- [ ] 1. Explore the app: auth, routing, theming, where global providers mount
- [ ] 2. Decide persistence (user record vs localStorage) — ask if unclear
- [ ] 3. Foundation: pnpm add driver.js, persistence fields, OnboardingProvider, tour catalog types
- [ ] 4. Add data-tour anchors to owned components
- [ ] 5. Write the tour catalog (5-8 steps per area, copy in the app's language)
- [ ] 6. Welcome wizard
- [ ] 7. Quick actions row
- [ ] 8. Help center view + guides data + FAB
- [ ] 9. Theme driver.js popovers + wizard + FAB to the app's brand
- [ ] 10. Verify with a fresh user end-to-end (see Verification)
```

### 1–2. Explore and decide persistence

Find: how the logged-in user is read client-side (Payload: `useAuth` from `@payloadcms/ui`; NextAuth: `useSession`; custom context), where a global client provider can mount (Payload: `admin.components.providers`; otherwise root layout), and the app's theming system (SCSS/Tailwind/CSS vars).

Persistence options (prefer the first when the app has users):

- **User record** (recommended): an `onboarding` object on the user — `welcomeCompletedAt` (ISO date), `toursCompleted` (string[]), `hideHints` (boolean). State follows the person across devices and an admin can reset it by clearing fields. In the admin UI, show these fields only to admins.
- **localStorage**: same shape under one key. Use only when there's no user store or the user prefers zero backend changes.

### 3. Foundation

- `pnpm add driver.js` (framework-agnostic, no React version coupling, ~5kB).
- One client `OnboardingProvider` mounted on every route. It owns: state hydration from the user, optimistic persist (PATCH the user, fire-and-forget), tour engine, wizard open state, FAB rendering, and URL params (`?tour=<id>`, `?wizard=1`).
- Tour catalog as **pure data** in `tours.ts` (no driver.js import) so server components like the help center can list tours too.

Full code templates: see [REFERENCE.md](REFERENCE.md).

### 4. Anchors

Add `data-tour="..."` attributes to components you own — stable across framework upgrades. For framework-rendered chrome you don't own (e.g. Payload's `.nav-group`, `.doc-controls`), use its DOM classes and note they may break on upgrades. Selectors like `#field-title` from form libraries are fine too.

### 5. Tour catalog rules

- 3–8 steps per tour. One tour per "area" with an `id`, `label`, `description`, canonical `href`, a `match: RegExp` against the pathname, and `steps`.
- Reuse existing in-app copy (field descriptions, helper text) so tours and forms speak the same language. Write in the app's UI language.
- Tone: explain *why* a thing matters and what to do, not just what it is.

### Hard-won engine rules (don't skip these)

These came from real debugging — bake them all into the provider:

- **Filter missing/hidden steps at start time**: drop any step whose `document.querySelector(el)` is null or has no client rects. A missing anchor degrades to a shorter tour, never a broken one.
- **600ms initial delay + retry loop**: client-hydrated components (rich text editors, lazy forms) mount after navigation. Delay the first element check ~600ms, then retry up to ~8 × 500ms if *zero* anchored steps exist yet. Without the initial delay, late-mounting steps get silently filtered out of an otherwise-startable tour.
- **Viewport gate**: skip auto-start below ~900px width — sidebar anchors are usually collapsed and the spotlight breaks. Manual launch can still work.
- **Closing early counts as seen**: persist completion in driver's `onDestroyed`, regardless of how far the user got. Never re-nag; the tour stays one click away in the help center.
- **`?tour=<id>` param**: lets the help center and quick actions relaunch a tour cross-route — navigate to `tour.href?tour=id`; the provider detects it, strips the param with `router.replace` (so refresh doesn't restart it), and runs the tour. Same pattern with `?wizard=1` for the wizard.
- **Cross-route start**: `startTour(id)` runs in place when `tour.match.test(pathname)`, otherwise pushes `href?tour=id`.
- **Teardown on navigation**: destroy the driver instance in a `useEffect` cleanup keyed on pathname.
- **Wizard gate for tours**: never auto-start a tour while the wizard hasn't been completed, and respect `hideHints`.
- **Hydrate once per login**: mirror user state into local state + a ref (the ref so async persists always send the full latest object, avoiding partial-merge ambiguity).

### 6. Wizard

4 steps max, each with icon + eyebrow + title + short body. Last step shows the same quick-action cards as the dashboard; picking one closes the wizard, persists `welcomeCompletedAt`, navigates. Every exit path (X, skip, finish, pick) persists — it never auto-shows twice. Personalize the title with the user's first name if available.

### 9. Theming

Style `.driver-popover.your-class` (set `popoverClass` in driver config), the wizard overlay/card, the FAB, and quick-action cards using the app's existing brand tokens. Include dark-mode overrides if the app has them. See [REFERENCE.md](REFERENCE.md) for the CSS skeleton. Import `driver.js/dist/driver.css` in the provider, then override.

If Payload: register new components in the import map (`pnpm generate:importmap`).

## Verification

Test with a **fresh user** (create a throwaway, delete after) using browser automation:

1. First login → wizard appears; completing/skipping persists; plain reload never shows it again; `?wizard=1` re-opens it (and strips the param).
2. Each tour auto-starts on first visit to its area and every step's anchor resolves. Programmatically step through: click `.driver-popover-next-btn` until done, collecting `.driver-popover-title` texts.
3. Completed tours don't re-trigger; relaunch works from help center and FAB.
4. Narrow viewport (use CDP `Emulation.setDeviceMetricsOverride` if the embedded browser is small — it often is ~545px, below the auto-start gate): no auto-start below the gate; FAB menu still usable at 1024px.
5. Production build passes.
6. Clean up: delete the test user and any drafts created (watch for autosave-created documents).
